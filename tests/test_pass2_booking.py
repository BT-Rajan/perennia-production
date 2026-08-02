"""
Pass 2 booking engine acceptance tests (docs/06-development-passes.md).
Against real MySQL, same pattern as test_pass1_foundation.py.
"""
import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.control.models import PlanTier, Tenant
from app.control.provisioning import provision_tenant
from app.core.db import get_control_session, get_tenant_session, init_control_db
from app.core.notify import LoggingNotifier, set_notifier, get_notifier
from app.core import booking
from app.security import decrypt_secret
from app.tenant.models import Staff, Service, AppointmentStatus, DepositStatus, CreatedVia


def _admin_conn():
    return pymysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
                            user=settings.MYSQL_ADMIN_USER, password=settings.MYSQL_ADMIN_PASSWORD,
                            autocommit=True)


def _cleanup_tenant(subdomain: str, db_name: str, db_user: str):
    conn = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            cur.execute(f"DROP USER IF EXISTS '{db_user}'@'%'")
    finally:
        conn.close()



@pytest.fixture(autouse=True)
def _reset_notifier():
    set_notifier(LoggingNotifier())
    yield


def _provision(subdomain: str) -> Tenant:
    with get_control_session() as cs:
        result = provision_tenant(cs, business_name=f"Test {subdomain}",
                                   subdomain=subdomain, plan_tier=PlanTier.growth)
        assert result.failed_step is None, f"provisioning failed: {result.error}"
        tenant_id = result.tenant.id
    with get_control_session() as cs:
        return cs.query(Tenant).filter_by(id=tenant_id).one()


class TestMultiStaffBooking:
    def test_two_staff_different_services_durations_both_bookable(self):
        t = _provision("pytest-booking-alpha")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                facial = Service(name="Facial", duration_minutes=60, price=15)
                manicure = Service(name="Manicure", duration_minutes=30, price=8)
                layla = Staff(name="Layla", gender="female", calendar_connected=True)
                rania = Staff(name="Rania", gender="female", calendar_connected=True)
                layla.services.append(facial)
                rania.services.append(manicure)
                ts.add_all([facial, manicure, layla, rania])
                ts.flush()
                layla_id, rania_id, facial_id, manicure_id = layla.id, rania.id, facial.id, manicure.id

            with get_tenant_session(t, pw) as ts:
                facial_staff = booking.bookable_staff_for_service(ts, facial_id)
                manicure_staff = booking.bookable_staff_for_service(ts, manicure_id)
            assert [s.id for s in facial_staff] == [layla_id]
            assert [s.id for s in manicure_staff] == [rania_id]

            import datetime
            tomorrow = (datetime.date.today() + datetime.timedelta(days=1))
            while tomorrow.weekday() >= 5:  # skip to a weekday for a deterministic test
                tomorrow += datetime.timedelta(days=1)

            with get_tenant_session(t, pw) as ts:
                facial_slots = booking.available_slots(ts, layla_id, facial_id, tomorrow.isoformat())
                manicure_slots = booking.available_slots(ts, rania_id, manicure_id, tomorrow.isoformat())
            assert len(facial_slots) > 0
            assert len(manicure_slots) > 0
            # Durations differ: a facial slot spans 60 min, manicure 30 min.
            f0 = facial_slots[0]
            import datetime as dt
            f_start = dt.datetime.fromisoformat(f0["start"])
            f_end = dt.datetime.fromisoformat(f0["end"])
            assert (f_end - f_start).total_seconds() == 3600
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestCalendarHardGate:
    def test_staff_without_connected_calendar_is_never_bookable(self):
        t = _provision("pytest-booking-beta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                massage = Service(name="Massage", duration_minutes=90, price=20)
                yousef = Staff(name="Yousef", gender="male", calendar_connected=False)  # NOT connected
                yousef.services.append(massage)
                ts.add_all([massage, yousef])
                ts.flush()
                yousef_id, massage_id = yousef.id, massage.id

            with get_tenant_session(t, pw) as ts:
                bookable = booking.bookable_staff_for_service(ts, massage_id)
                assert bookable == []

                slots = booking.available_slots(ts, yousef_id, massage_id, "2027-01-04")
                assert slots == []

                from app.core.booking import get_or_create_customer, StaffNotBookableError
                customer = get_or_create_customer(ts, phone="+96500000001")
                with pytest.raises(StaffNotBookableError):
                    booking.create_appointment(
                        ts, customer=customer, staff_id=yousef_id, service_id=massage_id,
                        start_iso="2027-01-04T10:00:00", deposit_required=False, actor="test",
                    )
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestDepositGating:
    def test_no_confirmed_appointment_without_successful_deposit(self):
        t = _provision("pytest-booking-gamma")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                facial = Service(name="Facial", duration_minutes=60, price=15)
                layla = Staff(name="Layla", calendar_connected=True)
                layla.services.append(facial)
                ts.add_all([facial, layla])
                ts.flush()
                layla_id, facial_id = layla.id, facial.id

            import datetime
            tomorrow = (datetime.date.today() + datetime.timedelta(days=1))
            while tomorrow.weekday() >= 5:
                tomorrow += datetime.timedelta(days=1)

            with get_tenant_session(t, pw) as ts:
                slots = booking.available_slots(ts, layla_id, facial_id, tomorrow.isoformat())
                start_iso = slots[0]["start"]
                customer = booking.get_or_create_customer(ts, phone="+96500000002", name="Noor")
                appt = booking.create_appointment(
                    ts, customer=customer, staff_id=layla_id, service_id=facial_id,
                    start_iso=start_iso, deposit_required=True, deposit_amount=5,
                    created_via=CreatedVia.chat, actor="chat-bot",
                )
                appt_id = appt.id
                assert appt.status == AppointmentStatus.pending_deposit
                assert appt.deposit_status == DepositStatus.pending

            # Failed payment: still not confirmed.
            with get_tenant_session(t, pw) as ts:
                booking.fail_deposit_payment(ts, appt_id, reason="card_declined", actor="payment-webhook")

            with get_tenant_session(t, pw) as ts:
                from app.tenant.models import Appointment
                row = ts.query(Appointment).filter_by(id=appt_id).one()
                assert row.status == AppointmentStatus.pending_deposit  # NOT confirmed

            # Successful payment: now confirmed, and a reminder was sent.
            with get_tenant_session(t, pw) as ts:
                booking.confirm_deposit_payment(ts, appt_id, provider_ref="pay_test_123", actor="payment-webhook")

            with get_tenant_session(t, pw) as ts:
                from app.tenant.models import Appointment
                row = ts.query(Appointment).filter_by(id=appt_id).one()
                assert row.status == AppointmentStatus.confirmed
                assert row.deposit_status == DepositStatus.paid

            notifier = get_notifier()
            assert any(e["type"] == "reminder" for e in notifier.sent)
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)

    def test_no_deposit_required_confirms_immediately(self):
        t = _provision("pytest-booking-delta")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                manicure = Service(name="Manicure", duration_minutes=30, price=8)
                rania = Staff(name="Rania", calendar_connected=True)
                rania.services.append(manicure)
                ts.add_all([manicure, rania])
                ts.flush()
                rania_id, manicure_id = rania.id, manicure.id

            import datetime
            tomorrow = (datetime.date.today() + datetime.timedelta(days=1))
            while tomorrow.weekday() >= 5:
                tomorrow += datetime.timedelta(days=1)

            with get_tenant_session(t, pw) as ts:
                slots = booking.available_slots(ts, rania_id, manicure_id, tomorrow.isoformat())
                customer = booking.get_or_create_customer(ts, phone="+96500000003")
                appt = booking.create_appointment(
                    ts, customer=customer, staff_id=rania_id, service_id=manicure_id,
                    start_iso=slots[0]["start"], deposit_required=False, actor="admin",
                )
                assert appt.status == AppointmentStatus.confirmed
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)


class TestDoubleBookingPrevention:
    def test_cannot_book_the_same_staff_slot_twice(self):
        t = _provision("pytest-booking-epsilon")
        try:
            pw = decrypt_secret(t.db_pass_encrypted)
            with get_tenant_session(t, pw) as ts:
                facial = Service(name="Facial", duration_minutes=60, price=15)
                layla = Staff(name="Layla", calendar_connected=True)
                layla.services.append(facial)
                ts.add_all([facial, layla])
                ts.flush()
                layla_id, facial_id = layla.id, facial.id

            import datetime
            tomorrow = (datetime.date.today() + datetime.timedelta(days=1))
            while tomorrow.weekday() >= 5:
                tomorrow += datetime.timedelta(days=1)

            with get_tenant_session(t, pw) as ts:
                slots = booking.available_slots(ts, layla_id, facial_id, tomorrow.isoformat())
                start_iso = slots[0]["start"]
                c1 = booking.get_or_create_customer(ts, phone="+96500000004")
                booking.create_appointment(ts, customer=c1, staff_id=layla_id, service_id=facial_id,
                                            start_iso=start_iso, deposit_required=False, actor="admin")

            with get_tenant_session(t, pw) as ts:
                c2 = booking.get_or_create_customer(ts, phone="+96500000005")
                with pytest.raises(booking.SlotUnavailableError):
                    booking.create_appointment(ts, customer=c2, staff_id=layla_id, service_id=facial_id,
                                                start_iso=start_iso, deposit_required=False, actor="admin")
        finally:
            _cleanup_tenant(t.subdomain, t.db_name, t.db_user)
