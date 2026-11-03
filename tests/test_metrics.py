"""The critic: does the score actually prefer better photographs?"""
from __future__ import annotations

import copy

import numpy as np
import pytest

from kestrel.models import EditPlan
from kestrel.render import metrics as M
from kestrel.render.core import RenderConfig, Renderer


@pytest.fixture(scope="module")
def renderer():
    return Renderer(RenderConfig(max_edge=760, quality="high", noise_refine=True))


def _mask_dict(masks):
    return {"subject": masks.subject, "background": masks.background,
            "face": masks.face, "skin": masks.skin, "grass": masks.grass, "crowd": masks.crowd}


def _plan(**kw):
    plan = EditPlan(genre="sports", intent="automatic", quality="high", iterations=2)
    for k, v in kw.items():
        setattr(plan.global_, k, v)
    return plan


def test_score_is_bounded_and_decomposed(analysis, renderer, engine, imported):
    a, masks, decoded = analysis
    plan, _, _ = engine.build_plan(imported, intent="automatic", quality="standard",
                                   analysis=a, masks=masks)
    out, _ = renderer.render(decoded.rgb, plan, _mask_dict(masks))
    m = M.compute_metrics(decoded.rgb, out, _mask_dict(masks), plan.genre, plan.intent)
    assert 0.0 <= m.score <= 1.0
    assert 0.0 <= m.naturalness <= 1.0
    assert m.contrast >= 0 and m.saturation >= 0
    assert m.noise_level >= 0 and m.haloing >= 0


def test_a_reasonable_edit_scores_above_extremes(analysis, renderer, engine, imported):
    a, masks, decoded = analysis
    base, _, _ = engine.build_plan(imported, quality="standard", analysis=a, masks=masks)
    md = _mask_dict(masks)

    good, _ = renderer.render(decoded.rgb, base, md)
    over, _ = renderer.render(decoded.rgb, _plan(exposure=2.2, contrast=90, saturation=80,
                                                 vibrance=80, clarity=70), md)
    none, _ = renderer.render(decoded.rgb, _plan(), md)

    m_good = M.compute_metrics(decoded.rgb, good, md, "sports", "automatic")
    m_over = M.compute_metrics(decoded.rgb, over, md, "sports", "automatic")
    m_none = M.compute_metrics(decoded.rgb, none, md, "sports", "automatic")
    assert m_good.score > m_over.score, (m_good.score, m_over.score)
    assert m_good.score > m_none.score, (m_good.score, m_none.score)
    assert m_over.naturalness < m_good.naturalness


def test_metrics_detect_clipping(analysis, renderer):
    """The metric must notice blown highlights wherever they come from."""
    _, masks, decoded = analysis
    md = _mask_dict(masks)
    blown = decoded.rgb.copy()
    blown[:80, :120] = 1.0                       # a genuinely clipped area
    m = M.compute_metrics(decoded.rgb, blown, md, "sports", "automatic")
    assert 0.005 < m.clipping_high < 0.05
    assert m.score < 0.9

    # The renderer's highlight handling keeps a 4 EV lift from clipping at all;
    # that is a property worth asserting in its own right.
    lifted, _ = renderer.render(decoded.rgb, _plan(exposure=4.0, highlights=100, whites=100), md)
    m_lift = M.compute_metrics(decoded.rgb, lifted, md, "sports", "automatic")
    assert m_lift.clipping_high <= 0.002


def test_metrics_penalise_oversaturation(analysis, renderer):
    _, masks, decoded = analysis
    md = _mask_dict(masks)
    plain, _ = renderer.render(decoded.rgb, _plan(), md)
    vivid, _ = renderer.render(decoded.rgb, _plan(saturation=95, vibrance=95), md)
    m_plain = M.compute_metrics(decoded.rgb, plain, md, "sports", "automatic")
    m_vivid = M.compute_metrics(decoded.rgb, vivid, md, "sports", "automatic")
    assert m_vivid.saturation > m_plain.saturation + 0.05
    assert m_vivid.score < m_plain.score, "wild saturation must not score better"
    # _accept_range returns a penalty: zero inside the window, positive outside
    assert M._accept_range(0.5, 0.3, 0.7) == 0.0
    assert M._accept_range(0.9, 0.3, 0.7) > 0.0
    assert M._accept_range(0.1, 0.3, 0.7) > 0.0


def test_skin_delta_e_is_near_zero_for_an_unchanged_frame(analysis, renderer):
    a, masks, decoded = analysis
    md = _mask_dict(masks)
    if masks.skin is None or float(masks.skin.sum()) < 500:
        pytest.skip("no skin region in this frame")
    out, _ = renderer.render(decoded.rgb, _plan(), md)
    m = M.compute_metrics(decoded.rgb, out, md, "sports", "automatic")
    assert m.skin_delta_e < 8.0


def test_refine_plan_is_bounded_and_explains_itself(analysis, renderer, engine, imported):
    a, masks, decoded = analysis
    plan, _, _ = engine.build_plan(imported, quality="standard", analysis=a, masks=masks)
    md = _mask_dict(masks)
    out, _ = renderer.render(decoded.rgb, plan, md)
    m = M.compute_metrics(decoded.rgb, out, md, plan.genre, plan.intent)
    m.clipping_high = 0.05                  # force a correction path
    refined = M.refine_plan(copy.deepcopy(plan), m, plan.genre, plan.intent, md)
    assert refined.global_.exposure <= plan.global_.exposure
    assert refined.global_.highlights <= plan.global_.highlights
    assert any(item.area == "refinement" for item in refined.explanation)
    for item in refined.explanation:
        assert item.reason and item.action


def test_refine_plan_never_runs_away(analysis, renderer, engine, imported):
    a, masks, decoded = analysis
    plan, _, _ = engine.build_plan(imported, quality="standard", analysis=a, masks=masks)
    md = _mask_dict(masks)
    m = M.compute_metrics(decoded.rgb, decoded.rgb, md, plan.genre, plan.intent)
    current = copy.deepcopy(plan)
    for _ in range(8):
        current = M.refine_plan(current, m, current.genre, current.intent, md)
    g = current.global_
    assert -3 <= g.exposure <= 3
    assert -100 <= g.contrast <= 100
    assert -100 <= g.highlights <= 100
    assert -40 <= g.saturation <= 40


def test_halo_metric_notices_a_ringing_edge():
    h, w = 200, 260
    base = np.full((h, w, 3), 0.4, np.float32)
    base[:, w // 2:] = 0.6
    ring = base.copy()
    ring[:, w // 2 - 2] = 0.15
    ring[:, w // 2 + 1] = 0.95
    md = {}
    m_clean = M.compute_metrics(base, base, md, "general", "natural")
    m_ring = M.compute_metrics(base, ring, md, "general", "natural")
    assert m_ring.haloing > m_clean.haloing


def test_shadow_crush_only_penalised_when_detail_existed(analysis, renderer):
    _, masks, decoded = analysis
    md = _mask_dict(masks)
    crushed, _ = renderer.render(decoded.rgb, _plan(blacks=-100, shadows=-100, exposure=-1.0), md)
    m = M.compute_metrics(decoded.rgb, crushed, md, "sports", "automatic")
    assert m.clipping_low > 0.0
