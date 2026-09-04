"""S03 微小領域。

2段構えにする。annotation 全体の面積だけを見ると、
**38万pxの本体に1〜2pxの飛びカスが付いている**ようなケースが絶対に見えない。
実際に morinomiyako の1件がその形。

面積は原則 mm²。spacing が施設・機種で 0.0875〜0.2 と 5.2倍ぶれるので、
px のままでは同じ画素数が施設によって5倍違う面積を指してしまう。

★severity は error ではなく warning。20mm² は仕様上の禁止値ではなく
**実測分布上の外れ値**（全クラスの実測最小 22.88mm² を下回り、1px と 747px の
間に747倍の空白がある、という統計的な根拠しかない）。
自動exclude はせず、目視で判断する。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK
from ..base import Category, CheckContext, CheckStatus, Issue, ReviewPriority, Severity
from ..machine._common import for_role, readable

CHECK_ID = "S03_TINY_ANNOTATION"
CATEGORY = Category.SUSPICIOUS
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "微小領域"
DESCRIPTION = "annotation 全体と連結成分の両方について、面積が小さすぎるものを拾う。"

TINY = "S03_TINY_ANNOTATION"
STRAY = "S03_STRAY_COMPONENT"
SUSPICIOUSLY_SMALL = "S03_SUSPICIOUSLY_SMALL"
NO_SPACING = "S03_NO_SPACING"


def run(ctx: CheckContext) -> Iterator[Issue]:
    thresholds = ctx.config.thresholds
    for record, measurement in for_role(ctx, ROLE_MASK):
        if not readable(measurement) or measurement.fg_pixels is None:
            continue

        if measurement.area_mm2 is None:
            yield ctx.issue(
                NO_SPACING,
                record,
                "spacing が無いので面積を mm² で評価できない",
                category=CATEGORY,
                severity=Severity.INFO,
                status=CheckStatus.CANNOT_DETERMINE,
                review_priority=ReviewPriority.LOW,
                cannot_determine_reason="no_spacing",
                fg_pixels=measurement.fg_pixels,
            )
            continue

        area = measurement.area_mm2
        if area < thresholds.tiny_annotation_mm2:
            yield ctx.issue(
                TINY,
                record,
                f"面積が全クラスの実測最小を下回る: {area:.3f}mm² "
                f"< {thresholds.tiny_annotation_mm2}mm²（{measurement.fg_pixels}px）",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.CRITICAL,
                area_mm2=area,
                fg_pixels=measurement.fg_pixels,
                threshold_mm2=thresholds.tiny_annotation_mm2,
            )
        else:
            yield from _class_conditional(ctx, record, measurement, area)

        yield from _stray_components(ctx, record, measurement)


def _class_conditional(ctx, record, measurement, area):
    """クラス族ごとの下限。

    面積分布は log 空間で滑らかな連続分布で、恣意的でない切れ目は無い。
    そこで一律ではなくクラス族ごとに下限を置き、その3倍までをレビュー帯とする。
    一律 <100mm² だと58件のうち43件が結節（正当に小さい）で予算を食われる。
    """
    thresholds = ctx.config.thresholds
    family = record.label_family
    floor = thresholds.small_by_class_mm2.get(family)
    if floor is None:
        return

    if area < floor:
        yield ctx.issue(
            SUSPICIOUSLY_SMALL,
            record,
            f"{family} の実測下限を下回る: {area:.1f}mm² < {floor}mm²",
            category=CATEGORY,
            severity=Severity.WARNING,
            review_priority=ReviewPriority.HIGH,
            area_mm2=area,
            label_family=family,
            threshold_mm2=floor,
            band="below_floor",
        )
    elif area < floor * thresholds.small_review_band_factor:
        yield ctx.issue(
            SUSPICIOUSLY_SMALL,
            record,
            f"{family} のレビュー帯: {area:.1f}mm²（下限 {floor}mm² の"
            f"{thresholds.small_review_band_factor}倍まで）",
            category=CATEGORY,
            severity=Severity.INFO,
            review_priority=ReviewPriority.NORMAL,
            area_mm2=area,
            label_family=family,
            threshold_mm2=floor,
            band="review_band",
        )


def _stray_components(ctx, record, measurement):
    """本体に付いた微小な連結成分（飛びカス）。

    ``region_count > 1`` の annotation だけが対象で、実データでは6件しかない。
    ここは実測で**真の二峰性**がある: 飛びカスは 0.02-0.79mm²、
    正当な副成分の最小は 174.2mm²。閾値をこの間のどこに置いても同じ集合になる。
    """
    thresholds = ctx.config.thresholds
    if (measurement.n_components or 0) <= 1:
        return
    total_pixels = measurement.fg_pixels or 0
    areas_mm2 = measurement.component_areas_mm2
    areas_px = measurement.component_areas

    for index, area_mm2 in enumerate(areas_mm2):
        area_px = areas_px[index] if index < len(areas_px) else 0
        ratio = area_px / total_pixels if total_pixels else 0.0
        if (
            area_mm2 >= thresholds.stray_component_mm2
            and ratio >= thresholds.stray_component_ratio
        ):
            continue
        yield ctx.issue(
            STRAY,
            record,
            f"本体に微小な連結成分が付いている: {area_mm2:.4f}mm² "
            f"({area_px}px, 全体の{ratio:.5f})",
            category=CATEGORY,
            severity=Severity.WARNING,
            review_priority=ReviewPriority.CRITICAL,
            component_area_mm2=area_mm2,
            component_area_px=area_px,
            component_ratio=ratio,
            n_components=measurement.n_components,
            annotation_area_mm2=measurement.area_mm2,
        )
