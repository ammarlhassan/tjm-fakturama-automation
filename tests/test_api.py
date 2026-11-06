import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from image_to_cash.api import create_app
from image_to_cash.models import Stage


def image_bytes(color="white"):
    buffer = io.BytesIO()
    Image.new("RGB", (200, 200), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def client(settings):
    api = create_app(settings)
    with TestClient(api) as client:
        yield client


def test_http_submission_and_idempotency(client):
    arguments = {
        "files": {"image": ("order.png", image_bytes(), "image/png")},
        "data": {"workspace": "demo"},
        "headers": {"idempotency-key": "one"},
    }
    first = client.post("/jobs", **arguments)
    assert first.status_code == 202
    second = client.post("/jobs", **arguments)
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["duplicate"]
    conflict = client.post(
        "/jobs",
        files={"image": ("other.png", image_bytes("red"), "image/png")},
        data={"workspace": "demo"},
        headers={"idempotency-key": "one"},
    )
    assert conflict.status_code == 409
    assert client.get(first.json()["status_url"]).json()["stage"] == "RECEIVED"


def test_key_accepted_for_a_duplicate_image_cannot_be_reused_for_another(client):
    def submit(color, key):
        return client.post(
            "/jobs",
            files={"image": ("order.png", image_bytes(color), "image/png")},
            data={"workspace": "demo"},
            headers={"idempotency-key": key},
        )

    first = submit("white", "one")
    second = submit("white", "two")
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["duplicate"]
    assert submit("red", "two").status_code == 409
    assert not submit("red", "three").json()["duplicate"]


def test_rejects_pdf_and_path_traversal(client):
    assert (
        client.post(
            "/jobs",
            files={"image": ("x.pdf", b"%PDF-1.7 fake", "application/pdf")},
            data={"workspace": "demo"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/jobs",
            files={"image": ("x.png", image_bytes(), "image/png")},
            data={"workspace": "../outside"},
        ).status_code
        == 422
    )


def test_review_is_explicit_and_preserves_original(client, source):
    job = client.post(
        "/jobs", files={"image": ("x.png", image_bytes(), "image/png")}, data={"workspace": "demo"}
    ).json()["job_id"]
    app = client.app.state.application
    app.store.update(
        job,
        stage=Stage.NEEDS_REVIEW,
        source=source.model_dump(mode="json"),
        review={"code": "OCR_DISAGREEMENT", "resolved": False},
    )
    assert client.post(f"/jobs/{job}/resume").status_code == 409
    correction = source.model_copy(deep=True)
    correction.customer.alias = "REVIEWED"
    assert (
        client.post(
            f"/jobs/{job}/review",
            json={
                "reason": "Compared the alias to the visible image",
                "source": correction.model_dump(mode="json"),
            },
        ).status_code
        == 200
    )
    history = app.store.events(job)
    assert history[-1]["data"]["before"]["customer"]["alias"] == "NORTHSTAR-BERLIN"
    assert history[-1]["data"]["after"]["customer"]["alias"] == "REVIEWED"
    assert client.post(f"/jobs/{job}/resume").status_code == 202


def test_cross_origin_mutations_are_rejected(client):
    assert client.post("/jobs", headers={"origin": "https://unrelated.example"}).status_code == 403


def test_missing_jobs_are_404(client):
    assert client.get("/jobs/missing").status_code == 404


def test_technical_extraction_retry_does_not_require_invented_source(client):
    job = client.post(
        "/jobs", files={"image": ("x.png", image_bytes(), "image/png")}, data={"workspace": "demo"}
    ).json()["job_id"]
    service = client.app.state.application
    service.store.update(job, stage=Stage.NEEDS_REVIEW, review={"code": "API_KEY_MISSING"})
    response = client.post(
        f"/jobs/{job}/review", json={"reason": "Configured the provider locally"}
    )
    assert response.status_code == 200
    assert service.store.get(job)["source"] is None
    assert service.store.events(job)[-1]["action"] == "technical_review"
    assert client.post(f"/jobs/{job}/resume").status_code == 202
    assert service.store.get(job)["stage"] == Stage.RECEIVED
