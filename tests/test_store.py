import pytest
from sqlalchemy.orm import Session

from image_to_cash.models import Stage
from image_to_cash.store import ConflictError, Job, Store


def test_same_image_is_deduplicated_per_workspace(store):
    first, duplicate = store.submit("demo", "a" * 64, "image.png")
    assert not duplicate
    assert store.submit("demo", "a" * 64, "image.png") == (first, True)
    assert store.submit("other", "a" * 64, "image.png")[0] != first


def test_idempotency_key_cannot_change_content(store):
    store.submit("demo", "a" * 64, "image.png", key="request-1")
    with pytest.raises(ConflictError):
        store.submit("demo", "b" * 64, "other.png", key="request-1")


def test_key_accepted_for_an_existing_image_is_bound_to_that_image(store, settings):
    job, _ = store.submit("demo", "a" * 64, "image.png", key="first")
    assert store.submit("demo", "a" * 64, "image.png", key="second") == (job, True)
    with pytest.raises(ConflictError):
        store.submit("demo", "b" * 64, "other.png", key="second")

    reopened = Store(settings.db_url)
    try:
        with pytest.raises(ConflictError):
            reopened.submit("demo", "b" * 64, "other.png", key="second")
        assert reopened.submit("demo", "a" * 64, "image.png", key="second") == (job, True)
    finally:
        reopened.engine.dispose()


def test_key_refused_for_a_mode_change_stays_available(store):
    store.submit("demo", "a" * 64, "image.png")
    with pytest.raises(ConflictError):
        store.submit("demo", "a" * 64, "image.png", key="request", mode="simulation")
    _, duplicate = store.submit("demo", "b" * 64, "other.png", key="request", mode="simulation")
    assert not duplicate


def test_key_recorded_on_an_older_job_is_still_honoured(store):
    with Session(store.engine) as session:
        session.add(
            Job(
                id="older",
                workspace="demo",
                image_hash="a" * 64,
                image_path="image.png",
                idempotency_key="request",
            )
        )
        session.commit()
    assert store.submit("demo", "a" * 64, "image.png", key="request") == ("older", True)
    with pytest.raises(ConflictError):
        store.submit("demo", "b" * 64, "other.png", key="request")


def test_one_key_cannot_bind_two_images_between_workers(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store.submit("demo", "a" * 64, "image.png")
    barrier = threading.Barrier(2)

    def submit(image_hash):
        barrier.wait()
        try:
            store.submit("demo", image_hash, "image.png", key="shared")
            return "accepted"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(submit, ["a" * 64, "b" * 64])) == ["accepted", "conflict"]


def test_atomic_claim_and_no_lease_takeover(store):
    job, _ = store.submit("demo", "a" * 64, "image.png")
    assert store.claim_job(job, "first")
    assert not store.claim_job(job, "second")
    store.own_workspace("demo", job, "first")
    with pytest.raises(ConflictError):
        store.own_workspace("demo", job, "second")


def test_same_reference_different_content_is_conflict(store, source):
    first, _ = store.submit("demo", "a" * 64, "image.png")
    second, _ = store.submit("demo", "b" * 64, "image.png")
    store.check_reference(first, source.external_reference, source.model_dump(mode="json"))
    source.customer.alias = "OTHER"
    with pytest.raises(ConflictError):
        store.check_reference(second, source.external_reference, source.model_dump(mode="json"))


def test_reference_reservation_is_atomic_between_workers(store, source):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    jobs = [store.submit("demo", str(index) * 64, "image.png")[0] for index in (1, 2)]
    barrier = threading.Barrier(2)

    def reserve(job):
        barrier.wait()
        try:
            store.check_reference(job, source.external_reference, source.model_dump(mode="json"))
            return "owned"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, jobs)) == ["conflict", "owned"]


def test_equivalent_decimal_formats_reuse_verified_receipt(store, source):
    from decimal import Decimal

    first = store.submit("demo", "a" * 64, "first.png")[0]
    second = store.submit("demo", "b" * 64, "second.png")[0]
    store.check_reference(first, source.external_reference, source.model_dump(mode="json"))
    store.update(first, stage=Stage.SUCCEEDED)
    source.items[0].unit_net = Decimal("250.0")
    source.items[0].quantity = Decimal("2.00")
    assert (
        store.check_reference(second, source.external_reference, source.model_dump(mode="json"))
        == first
    )


def test_simulation_receipt_cannot_satisfy_live_job(store, source):
    first = store.submit("demo", "a" * 64, "first.png", mode="simulation")[0]
    second = store.submit("demo", "b" * 64, "second.png", mode="live")[0]
    store.check_reference(first, source.external_reference, source.model_dump(mode="json"))
    store.update(first, stage=Stage.SUCCEEDED)
    with pytest.raises(ConflictError, match="execution modes"):
        store.check_reference(second, source.external_reference, source.model_dump(mode="json"))


def test_quarantine_blocks_other_live_workspaces_on_same_desktop(store):
    first = store.submit("first", "a" * 64, "first.png")[0]
    second = store.submit("second", "b" * 64, "second.png")[0]
    store.own_workspace("first", first, "worker-a", desktop=True)
    store.release_workspace("first", first, quarantine=True)
    with pytest.raises(ConflictError, match="Desktop session"):
        store.own_workspace("second", second, "worker-b", desktop=True)
