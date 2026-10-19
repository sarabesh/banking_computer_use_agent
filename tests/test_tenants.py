"""Tenant identification from the screen, not the URL.

The URL only says where to open the app; the registry holds no URLs. Which institution it is
gets decided from the first screen's SIGNATURE (institution name weighted most, plus page title
and the screen's labels/buttons), before anything is typed: a registered institution is
matched, an unknown one is registered so the next run matches it."""
import yaml

from cua.catalog import Catalog
from cua.replay import Replayer
from cua.surface.base import Element, Location, Observation
from cua.surface.web import WebSurface
from cua.tenants import Signature, Tenant, TenantRegistry, TenantResolver, guess_identity, score, signature_of

from .test_overrides import riverbend_override, riverbend_probe  # noqa: F401 - module fixtures reused
from .test_replay import capability, policy_for, serve  # noqa: F401


def login_screen(institution: str, version: str, title: str, labels: list[str]) -> Observation:
    els = [Element(ref="e1", kind="text", role="cell", name=f"{institution} CoreLine Teller {version}",
                   frame="main", frame_path="/login", tag="td", bbox=[450, 40, 380, 40]),
           Element(ref="e2", kind="text", role="text", name=institution, frame="main", frame_path="/login",
                   tag="b", bbox=[458, 45, 250, 18])]
    for i, label in enumerate(labels, start=3):
        els.append(Element(ref=f"e{i}", kind="text", role="text", name=label, frame="main", frame_path="/login",
                           tag="td", bbox=[460, 100 + 30 * i, 120, 20]))
    return Observation(seq=1, location=Location(url="/login", title=title, frames={"main": "/login"}), elements=els)


ACME = login_screen("Acme Community Credit Union", "7.4.2", "CoreLine Teller - Teller Sign On",
                    ["Teller Sign On", "User ID:", "Password:", "Sign On"])
# A clone of Acme's configuration (same product, version, title, labels) - only the name differs.
LAKESIDE = login_screen("Lakeside Savings Bank", "7.4.2", "CoreLine Teller - Teller Sign On",
                        ["Teller Sign On", "User ID:", "Password:", "Sign On"])
RIVERBEND = login_screen("Riverbend Federal Credit Union", "7.6.0", "CoreLine Teller - Staff Login",
                         ["Staff Login", "Operator:", "PIN / Password:", "Login"])


def registry(tmp_path, *tenants: Tenant) -> TenantRegistry:
    reg = TenantRegistry({t.id: t for t in tenants}, tmp_path / "tenants.yaml")
    reg.save()
    return reg


def test_guess_identity_takes_the_header_name_not_a_label_or_the_product_line():
    assert guess_identity(ACME) == "Acme Community Credit Union"
    assert guess_identity(RIVERBEND) == "Riverbend Federal Credit Union"


def test_a_clone_configuration_is_not_mistaken_for_the_original():
    """Same product, version, title and labels - only the name differs: the name dominates."""
    acme = Tenant("acme", "Acme Community Credit Union", signature=signature_of(ACME))
    shown = " | ".join(e.name for e in LAKESIDE.elements).casefold()
    value, missing, _new = score(acme, signature_of(LAKESIDE), shown)
    assert value < 0.75 and missing == []          # everything else matches, yet it is not Acme


def test_unknown_institution_is_registered_then_matched_next_time(tmp_path):
    reg = registry(tmp_path, Tenant("acme", "Acme Community Credit Union", signature=signature_of(ACME)))
    first = reg.identify(LAKESIDE)
    assert first.how == "registered" and first.tenant.id == "lakeside_savings_bank" and first.changed_registry
    assert first.tenant.auto_registered and first.tenant.signature is not None
    again = TenantRegistry.load(tmp_path / "tenants.yaml").identify(LAKESIDE)      # persisted
    assert again.how == "matched" and again.tenant.id == "lakeside_savings_bank" and not again.changed_registry
    saved = yaml.safe_load((tmp_path / "tenants.yaml").read_text(encoding="utf-8"))["tenants"]
    assert "base_url" not in str(saved) and "http" not in str(saved)               # no URLs stored anywhere


def test_signature_is_learned_for_entries_that_only_have_a_name(tmp_path):
    reg = registry(tmp_path, Tenant("acme", "Acme Community Credit Union"))
    ident = reg.identify(ACME)
    assert ident.how == "matched" and "signature recorded" in ident.note
    assert TenantRegistry.load(tmp_path / "tenants.yaml").tenants["acme"].signature is not None


def test_a_changed_screen_still_matches_and_reports_the_drift(tmp_path):
    reg = registry(tmp_path, Tenant("acme", "Acme Community Credit Union", signature=signature_of(ACME)))
    upgraded = login_screen("Acme Community Credit Union", "7.4.2", "CoreLine Teller - Teller Sign On",
                            ["Teller Sign On", "User ID:", "Password:", "Log In"])        # a button renamed
    ident = reg.identify(upgraded)
    assert ident.how == "matched" and ident.tenant.id == "acme"
    assert "missing ['sign on']" in ident.note and "log in" in ident.note
    assert TenantRegistry.load(tmp_path / "tenants.yaml").tenants["acme"].signature == signature_of(ACME)  # kept


def test_ambiguous_screen_is_not_guessed(tmp_path):
    both = Signature("t", frozenset({"x", "y"}))
    reg = registry(tmp_path, Tenant("a", "Shared Name", signature=both), Tenant("b", "Shared Name", signature=both))
    screen = login_screen("Shared Name", "1.0", "t", ["x", "y"])
    ident = reg.identify(screen)
    assert ident.tenant is None and ident.how == "unidentified" and "ambiguous" in ident.note


def test_replay_applies_the_override_of_the_institution_on_screen(capability, riverbend_override, tmp_path,  # noqa: F811
                                                                  secrets_env):
    """Whatever the URL, the Riverbend app gets Riverbend's override - identified from its screen."""
    _, server, river_url = serve("riverbend")
    overrides = tmp_path / "overrides"
    riverbend_override.model_copy(update={"status": "approved"}).save(overrides)
    reg = registry(tmp_path, Tenant("acme", "Acme Community Credit Union"),
                   Tenant("riverbend", "Riverbend Federal Credit Union"))
    policy = policy_for(river_url)
    replayer = Replayer(WebSurface(policy, headless=True), policy, tmp_path, echo=lambda _: None,
                        tenant_resolver=TenantResolver(reg, overrides))
    try:
        result = replayer.run(capability, {"member_number": "12345"}, base_url=river_url)
    finally:
        server.shutdown()
    assert result.status == "success" and result.tenant == "riverbend" and result.override
    assert result.low_confidence_steps == [] and "matches 'riverbend'" in result.tenant_note


def test_catalog_call_identifies_the_tenant_per_call(capability, tmp_path, secrets_env):  # noqa: F811
    _, server, river_url = serve("riverbend")
    caps = tmp_path / "capabilities"
    capability.save(caps)
    reg_path = tmp_path / "tenants.yaml"
    reg_path.write_text("tenants: {}\n", encoding="utf-8")                          # nobody registered yet
    catalog = Catalog(caps, tmp_path / "overrides", reg_path, river_url)
    try:
        result = catalog.invoke("member_savings_balance", {"member_number": "12345"},
                                policy=policy_for(river_url), runs_dir=tmp_path / "runs")
    finally:
        server.shutdown()
    assert result.tenant == "riverbend_federal_credit_union" and "registered" in result.tenant_note
    assert "riverbend_federal_credit_union" in reg_path.read_text(encoding="utf-8")
