"""Standalone metadata-driven experimental Axis Plotter panel.

Intended long-term design:

    - UI / plotting code consumes an AxesStream only.
    - AxesStream accessor functions are the only way UI / plotting code reads axes.
    - Raw metadata navigation is currently temporary and isolated in one section.
    - The temporary stream construction section can later be replaced by a real
      stream object supplied directly by metadata / niondata.

    - Select/focus a display panel.
    - The panel automatically loads axes from that display panel's data item.
    - Existing overlays on other data items remain visible.
    - Refresh remains as a manual fallback.
"""

from __future__ import annotations

import typing

from dataclasses import dataclass

from nion.typeshed import API_1_0 as Facade
from nion.utils import Geometry


T = typing.TypeVar("T")


def temporary(reason: str) -> typing.Callable[[T], T]:
    """Mark an object as temporary without changing runtime behaviour."""

    def decorator(obj: T) -> T:
        setattr(obj, "__temporary__", True)
        setattr(obj, "__temporary_reason__", reason)
        return obj

    return decorator


PANEL_ID = "experimental-axis-plotter"
PANEL_TITLE = "[Experimental] Axis plotter"

AXIS_TRANSFORMATION_MATRICES_METADATA_PATH = "instrument.axis_transformation_matrices"

TEMPORARY_STREAM_REASON = (
    "Temporary stream construction. Raw metadata navigation and parsing lives "
    "here until a real AxesStream exists directly in metadata/niondata."
)


# --------------------------------------------------------------------------------------
# Shared data types
# --------------------------------------------------------------------------------------

class ApiGraphicLike(typing.Protocol):
    """Minimal Swift API graphic/region interface used by overlay drawing."""

    def set_property(self, key: str, value: object) -> None:
        ...


class ApiXDataLike(typing.Protocol):
    """Minimal xdata interface used to obtain the active data shape."""

    @property
    def data_shape(self) -> typing.Sequence[int]:
        ...


class ApiDataItemLike(typing.Protocol):
    """Minimal Swift API data-item interface used by the axis plotter.

    uuid:
        UUID-like Swift facade value used as the overlay dictionary key. T
    metadata:
        Metadata mapping used only by the temporary stream-construction layer.
    xdata:
        XData facade exposing data_shape for display-vector normalisation.
    """

    @property
    def uuid(self) -> object:
        ...

    @property
    def metadata(self) -> typing.Mapping[object, object]:
        ...

    @property
    def xdata(self) -> ApiXDataLike:
        ...

    def add_line_region(self, start_y: float, start_x: float, end_y: float, end_x: float) -> ApiGraphicLike:
        ...

    def remove_region(self, graphic: ApiGraphicLike) -> None:
        ...


class ApiDisplayLike(typing.Protocol):
    """Minimal Swift API display interface used to find a data item."""

    @property
    def data_item(self) -> ApiDataItemLike | None:
        ...


class ApiDocumentWindowLike(typing.Protocol):
    """Minimal Swift API document-window interface used for active display lookup."""

    @property
    def target_display(self) -> ApiDisplayLike | None:
        ...

    @property
    def target_data_item(self) -> ApiDataItemLike | None:
        ...


class ApiApplicationLike(typing.Protocol):
    """Minimal root application interface exposing document windows."""

    @property
    def document_windows(self) -> typing.Sequence[ApiDocumentWindowLike]:
        ...


class ApiLike(typing.Protocol):
    """Minimal root Swift API interface used by this panel."""

    @property
    def application(self) -> ApiApplicationLike:
        ...


class ApiBrokerLike(typing.Protocol):
    """Minimal API broker protocol used by the extension entry point."""

    def get_api(self, version: str) -> Facade.API:
        ...


class EventListenerLike(typing.Protocol):
    """Minimal event-listener interface returned by Nion event listen calls."""

    def close(self) -> None:
        ...


StoredGraphic = tuple[ApiDataItemLike, ApiGraphicLike]
AxisGraphicKey = tuple[str, str]


@dataclass(frozen=True)
class CoordinateAxis:
    """Parsed coordinate-axis data used by the UI and overlay renderer."""

    axis_id: str
    display_name: str
    axis_type: tuple[str, str]
    origin: Geometry.FloatPoint
    x_vector: Geometry.FloatPoint
    y_vector: Geometry.FloatPoint
    color: str
    metadata_source: str


@dataclass(frozen=True)
class VisibleAxisOverlay:
    """Currently visible axis overlay graphics on one API data item."""
    data_item_key: str
    axis_id: str
    axis: CoordinateAxis
    graphics_data_item: ApiDataItemLike
    color: str
    graphics: tuple[StoredGraphic, ...]


# --------------------------------------------------------------------------------------
# AxesStream accessor functions
#
# UI and plotting code should only use these functions to read axes stream data.
# They define the stable stream-accessor boundary
# expected to remain when the real stream object moves into metadata / niondata.
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AxesStream:
    """Axes stream object consumed by UI / plotting code.

    source_data_item:
        Data item used to construct the stream.
    axes:
        Immutable mapping from axis id to parsed CoordinateAxis.
    metadata_source:
        Metadata source path used to construct the stream.
    """

    source_data_item: ApiDataItemLike
    axes: typing.Mapping[str, CoordinateAxis]
    metadata_source: str


def get_source_data_item_from_axes_stream(axes_stream: AxesStream) -> ApiDataItemLike:
    """Return the source data item used to build this axes stream."""

    return axes_stream.source_data_item


def get_axes_from_axes_stream(axes_stream: AxesStream) -> typing.Mapping[str, CoordinateAxis]:
    """Return all axes from an axes stream."""

    return axes_stream.axes


def get_axis_from_axes_stream(axes_stream: AxesStream, axis_id: str) -> CoordinateAxis | None:
    """Return a single axis from an axes stream."""

    return axes_stream.axes.get(axis_id)


def get_metadata_source_from_axes_stream(axes_stream: AxesStream) -> str:
    """Return the metadata source used to construct this axes stream."""

    return axes_stream.metadata_source
