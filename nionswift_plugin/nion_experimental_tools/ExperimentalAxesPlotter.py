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

import traceback
import types
import typing

from dataclasses import dataclass

from nion.swift import DocumentController
from nion.swift import Panel
from nion.swift import Workspace
from nion.swift.model import PlugInManager
from nion.typeshed import API_1_0 as Facade
from nion.ui import Declarative
from nion.utils import Geometry
from nion.utils import Stream


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


# --------------------------------------------------------------------------------------
# UI and plotting code - consumes AxesStream only
# --------------------------------------------------------------------------------------

class ExperimentalAxesPlotterHandler(Declarative.Handler):
    name = "Metadata Axis Plotter"

    def __init__(self, rebuild_widget_fn: typing.Callable[[], None]) -> None:
        super().__init__()

        api_broker = PlugInManager.APIBroker()
        facade_api = typing.cast(Facade.API, api_broker.get_api(version="~1.0"))
        self._api: ApiLike = typing.cast(ApiLike, facade_api)

        self._rebuild_widget_fn = rebuild_widget_fn
        self._current_display_item: ApiDisplayLike | None = None
        self._current_axes_stream: AxesStream | None = None

        self.axis_ids: list[str] = []

        self._axis_id_to_axis: dict[str, CoordinateAxis] = {}
        self._axis_id_to_safeid: dict[str, str] = {}
        self._safeid_to_axis_id: dict[str, str] = {}
        self._axis_graphics: dict[AxisGraphicKey, VisibleAxisOverlay] = {}
        self._axis_toggle_callback_names: set[str] = set()

        self.full_length_enabled = False
        self.status_text = "Select a display panel containing a data item."

        self._ui = Declarative.DeclarativeUI()
        self.ui_view = self._build_ui()

        self._axes_stream_stream = self._create_axes_value_stream()
        self._axes_stream_listener: EventListenerLike | None = (
            self._axes_stream_stream.value_stream.listen(self._axes_stream_changed)
        )

    def close(self) -> None:
        """Declarative widget close.

        Do not remove axis overlays here, otherwise overlays disappear on focus changes.
        """

        return

    def close_for_panel(self) -> None:
        """Clean up streams and overlays because the actual panel is closing."""

        if self._axes_stream_listener is not None:
            try:
                self._axes_stream_listener.close()
            except Exception:
                traceback.print_exc()

            self._axes_stream_listener = None

        self._remove_all_axis_graphics()

    def _create_axes_value_stream(self) -> Stream.ValueStream[AxesStream | None]:
        """Create the current AxesStream value stream."""

        try:
            return typing.cast(
                Stream.ValueStream[AxesStream | None],
                Stream.ValueStream(None)
            )
        except TypeError:
            axes_stream_stream = typing.cast(
                Stream.ValueStream[AxesStream | None],
                Stream.ValueStream()
            )
            axes_stream_stream.value = None
            return axes_stream_stream

    def _set_axes_stream_value(self, axes_stream: AxesStream | None) -> None:
        """Set the current axes stream and force the UI state to update."""

        try:
            self._axes_stream_stream.value = axes_stream
        except Exception:
            traceback.print_exc()

        self._axes_stream_changed(axes_stream)

    def _axes_stream_changed(self, axes_stream: AxesStream | None) -> None:
        """Refresh handler state when the current AxesStream changes."""

        self._current_axes_stream = axes_stream
        self._refresh_axes_from_axes_stream(axes_stream)
        self._rebuild_ui()

    def set_display_item(self, display_item: ApiDisplayLike | None) -> None:
        """Update axes from the selected/focused display item."""

        self._current_display_item = display_item
        self._set_axes_stream_value(self._create_axes_stream_for_current_display_item())

    def refresh_from_current_selection(self) -> None:
        """Manual fallback refresh using the current display item or API target."""

        self._set_axes_stream_value(self._create_axes_stream_for_current_display_item())

    def _notify_property_changed(self, property_name: str) -> None:
        try:
            self.property_changed_event.fire(property_name)
        except AttributeError:
            return
        except Exception:
            traceback.print_exc()

    def _rebuild_ui(self) -> None:
        self.ui_view = self._build_ui()
        self._rebuild_widget_fn()

    def _build_ui(self) -> Declarative.UIDescriptionResult:
        """Build the Declarative UI description in the original compact style."""

        u = self._ui

        header = u.create_row(
            u.create_label(text="Axis Plotter", width=220),
            u.create_push_button(text="Refresh", on_clicked="on_refresh_clicked", width=80),
            u.create_push_button(text="Clear All", on_clicked="on_clear_all_clicked", width=90),
            u.create_stretch(),
            spacing=8
        )

        options_row = u.create_row(
            u.create_check_box(
                text="Full length lines",
                checked="@binding(full_length_enabled)",
                tool_tip="Draw each axis as a full line centered on the origin."
            ),
            u.create_stretch(),
            spacing=8
        )

        if not self.axis_ids:
            body = u.create_column(
                u.create_label(text="@binding(status_text)", width=460),
                spacing=6
            )
        else:
            rows: list[Declarative.UIDescriptionResult] = []

            for axis_id in self.axis_ids:
                axis = self._axis_id_to_axis[axis_id]
                safeid = self._axis_id_to_safeid[axis_id]

                color_attr = f"axis_color_{safeid}"
                toggle_method = f"on_toggle_{safeid}_clicked"

                if not hasattr(self, toggle_method):
                    setattr(self, toggle_method, self._make_toggle_handler(axis_id))
                    self._axis_toggle_callback_names.add(toggle_method)

                axis_label = f"{axis.display_name} ({axis.axis_type[0]}, {axis.axis_type[1]})"

                rows.append(
                    u.create_row(
                        u.create_label(text=axis_label, width=160),
                        u.create_push_button(text="Toggle", on_clicked=toggle_method, width=70),
                        u.create_line_edit(text=f"@binding({color_attr})", width=90),
                        {"type": "nionswift.color_chooser", "color": f"@binding({color_attr})"},
                        u.create_stretch(),
                        spacing=8
                    )
                )

            body = u.create_column(
                u.create_label(text="@binding(status_text)", width=460),
                *rows,
                spacing=6
            )

        return u.create_column(header, options_row, body, spacing=10)

    def _make_toggle_handler(self, axis_id: str) -> typing.Callable[[Declarative.UIWidget], None]:
        """Create a direct button callback that toggles one axis."""

        def _handler(widget: Declarative.UIWidget) -> None:
            self._toggle_axis(axis_id)

        return _handler

    def _sanitize_axis_id(self, axis_id: str) -> str:
        out: list[str] = []

        for ch in axis_id:
            out.append(ch if ch.isalnum() else "_")

        safe_id = "".join(out)

        if safe_id and safe_id[0].isdigit():
            safe_id = "_" + safe_id

        return safe_id

    def _clear_axis_color_attributes(self) -> None:
        for attr_name in list(vars(self)):
            if attr_name.startswith("axis_color_"):
                object.__delattr__(self, attr_name)

    def _clear_axis_toggle_callbacks(self) -> None:
        for callback_name in list(self._axis_toggle_callback_names):
            if callback_name in self.__dict__:
                object.__delattr__(self, callback_name)

        self._axis_toggle_callback_names.clear()

    def _get_target_document_window(self) -> ApiDocumentWindowLike | None:
        windows = self._api.application.document_windows

        if not windows:
            return None

        for window in windows:
            if window.target_display is not None:
                return window

        return windows[0]

    def _get_active_data_item(self) -> ApiDataItemLike | None:
        window = self._get_target_document_window()

        if window is None:
            return None

        if window.target_data_item is not None:
            return window.target_data_item

        if window.target_display is not None:
            return window.target_display.data_item

        return None

    def _get_data_item_key(self, data_item: ApiDataItemLike) -> str:
        return str(data_item.uuid)

    def _create_axes_stream_for_current_display_item(self) -> AxesStream | None:
        if self._current_display_item is not None:
            return create_axes_stream_from_display_item(self._current_display_item)

        data_item = self._get_active_data_item()

        if data_item is None:
            return None

        return create_axes_stream_from_data_item(data_item)

    def _get_axes_stream(self) -> AxesStream | None:
        return self._current_axes_stream

    def _refresh_axes_from_axes_stream(self, axes_stream: AxesStream | None) -> None:
        """Refresh axis UI state from the current AxesStream.

        This clears current UI axis state, removes stale colour/callback bindings,
        sorts the axis ids, and repopulates the axis lookup for the current stream.
        Existing overlay graphics are intentionally preserved.
        """

        self.axis_ids.clear()
        self._axis_id_to_axis.clear()
        self._axis_id_to_safeid.clear()
        self._safeid_to_axis_id.clear()
        self._clear_axis_color_attributes()
        self._clear_axis_toggle_callbacks()

        if axes_stream is None:
            self.status_text = "Selected display panel has no readable axes metadata."
            self._notify_property_changed("status_text")
            return

        axes = get_axes_from_axes_stream(axes_stream)
        metadata_source = get_metadata_source_from_axes_stream(axes_stream)

        if not axes:
            self.status_text = "Selected display panel has no readable axes metadata."
            self._notify_property_changed("status_text")
            return

        preferred_order = (
            "tv",
            "scan",
            "stageaxis",
            "stagetiltaxis",
            "eels",
            "mc",
            "postsample",
            "correctoraxis",
            "gun"
        )

        ordered_axis_ids: list[str] = []

        for preferred_axis_id in preferred_order:
            if preferred_axis_id in axes:
                ordered_axis_ids.append(preferred_axis_id)

        for axis_id in axes:
            if axis_id not in ordered_axis_ids:
                ordered_axis_ids.append(axis_id)

        self.axis_ids[:] = ordered_axis_ids

        for axis_id in self.axis_ids:
            axis = axes[axis_id]
            safeid = self._sanitize_axis_id(axis_id)

            self._axis_id_to_axis[axis_id] = axis
            self._axis_id_to_safeid[axis_id] = safeid
            self._safeid_to_axis_id[safeid] = axis_id

            setattr(self, f"axis_color_{safeid}", axis.color)

        self.status_text = f"Loaded {len(self.axis_ids)} axes from {metadata_source}."
        self._notify_property_changed("status_text")

    def _shape_to_height_width(self, shape_value: object) -> tuple[int, int] | None:
        if isinstance(shape_value, (tuple, list)) and len(shape_value) >= 2:
            return int(shape_value[-2]), int(shape_value[-1])

        return None

    def _get_active_data_shape(self, data_item: ApiDataItemLike) -> tuple[int, int] | None:
        return self._shape_to_height_width(data_item.xdata.data_shape)

    def _normalize_vector(self, vector: Geometry.FloatPoint) -> Geometry.FloatPoint:
        length = float(abs(vector))

        if length <= 1e-12:
            return Geometry.FloatPoint(y=0.0, x=0.0)

        return Geometry.FloatPoint(y=vector.y / length, x=vector.x / length)

    def _normalize_vector_for_display(self, vector: Geometry.FloatPoint, data_shape: tuple[int, int] | None) -> Geometry.FloatPoint:
        if data_shape is None:
            return self._normalize_vector(vector)

        height, width = data_shape

        if height <= 0 or width <= 0:
            return self._normalize_vector(vector)

        pixel_y = vector.y * height
        pixel_x = vector.x * width
        pixel_length = (pixel_y * pixel_y + pixel_x * pixel_x) ** 0.5

        if pixel_length <= 1e-12:
            return Geometry.FloatPoint(y=0.0, x=0.0)

        scale = float(min(height, width))

        return Geometry.FloatPoint(
            y=(pixel_y / pixel_length) * (scale / height),
            x=(pixel_x / pixel_length) * (scale / width)
        )

    def _clamp_point(self, point: Geometry.FloatPoint) -> Geometry.FloatPoint:
        return Geometry.FloatPoint(
            y=min(max(point.y, 0.0), 1.0),
            x=min(max(point.x, 0.0), 1.0)
        )

    def _axis_line_points(self, origin: Geometry.FloatPoint, unit_vector: Geometry.FloatPoint, line_length: float, *, forward: bool = True) -> tuple[Geometry.FloatPoint, Geometry.FloatPoint]:
        """Return start/end points for a forward or backward half-axis line."""

        direction_sign = 1.0 if forward else -1.0

        end = self._clamp_point(
            Geometry.FloatPoint(
                y=origin.y + direction_sign * unit_vector.y * line_length,
                x=origin.x + direction_sign * unit_vector.x * line_length
            )
        )

        return origin, end

    def _make_line_region(self, data_item: ApiDataItemLike, start: Geometry.FloatPoint, end: Geometry.FloatPoint, color: str, label: str, *, arrow_at_end: bool = True) -> ApiGraphicLike:
        """Create and configure one Swift line-region overlay."""

        graphic = data_item.add_line_region(start.y, start.x, end.y, end.x)

        graphic.set_property("label", label)
        graphic.set_property("stroke_color", color)
        graphic.set_property("stroke_width", 2.0)
        graphic.set_property("start_arrow_enabled", False)
        graphic.set_property("end_arrow_enabled", arrow_at_end)

        return graphic

    def _append_axis_line_region(self, graphics_to_add: list[StoredGraphic], graphics_data_item: ApiDataItemLike, start: Geometry.FloatPoint, end: Geometry.FloatPoint, color: str, label: str, *, arrow_at_end: bool = True) -> None:
        graphic = self._make_line_region(
            graphics_data_item,
            start,
            end,
            color,
            label,
            arrow_at_end=arrow_at_end
        )
        graphics_to_add.append((graphics_data_item, graphic))

    def _show_axis_overlay(self, data_item_key: str, axis: CoordinateAxis, graphics_data_item: ApiDataItemLike, color: str) -> tuple[StoredGraphic, ...] | None:
        """Create overlay graphics for one axis on one API data item."""

        data_shape = self._get_active_data_shape(graphics_data_item)

        x_unit_vector = self._normalize_vector_for_display(axis.x_vector, data_shape)
        y_unit_vector = self._normalize_vector_for_display(axis.y_vector, data_shape)

        if abs(x_unit_vector) <= 1e-12 or abs(y_unit_vector) <= 1e-12:
            self.status_text = f"Cannot plot {axis.display_name}: metadata vector is near zero."
            self._notify_property_changed("status_text")
            return None

        line_length = 0.22
        axis_name_0, axis_name_1 = axis.axis_type
        graphics_to_add: list[StoredGraphic] = []

        try:
            x_forward_start, x_forward_end = self._axis_line_points(
                axis.origin,
                x_unit_vector,
                line_length,
                forward=True
            )
            y_forward_start, y_forward_end = self._axis_line_points(
                axis.origin,
                y_unit_vector,
                line_length,
                forward=True
            )

            self._append_axis_line_region(
                graphics_to_add,
                graphics_data_item,
                x_forward_start,
                x_forward_end,
                color,
                axis_name_0,
                arrow_at_end=True
            )
            self._append_axis_line_region(
                graphics_to_add,
                graphics_data_item,
                y_forward_start,
                y_forward_end,
                color,
                axis_name_1,
                arrow_at_end=True
            )

            if self.full_length_enabled:
                x_backward_start, x_backward_end = self._axis_line_points(
                    axis.origin,
                    x_unit_vector,
                    line_length,
                    forward=False
                )
                y_backward_start, y_backward_end = self._axis_line_points(
                    axis.origin,
                    y_unit_vector,
                    line_length,
                    forward=False
                )

                self._append_axis_line_region(
                    graphics_to_add,
                    graphics_data_item,
                    x_backward_start,
                    x_backward_end,
                    color,
                    "",
                    arrow_at_end=False
                )
                self._append_axis_line_region(
                    graphics_to_add,
                    graphics_data_item,
                    y_backward_start,
                    y_backward_end,
                    color,
                    "",
                    arrow_at_end=False
                )

        except Exception:
            for graphic_data_item, graphic in graphics_to_add:
                try:
                    graphic_data_item.remove_region(graphic)
                except Exception:
                    traceback.print_exc()

            traceback.print_exc()
            self.status_text = f"Failed to plot axis {axis.display_name}."
            self._notify_property_changed("status_text")
            return None

        return tuple(graphics_to_add)

    def _toggle_axis(self, axis_id: str) -> None:
        """Toggle one axis overlay on the active API data item.

        The available axis must come from the current AxesStream. Overlay state is
        keyed by data item and axis id so overlays remain visible when focus moves.
        """

        axes_stream = self._get_axes_stream()

        if axes_stream is None:
            self.status_text = "No axes stream available for the selected display panel."
            self._notify_property_changed("status_text")
            self._refresh_axes_from_axes_stream(None)
            self._rebuild_ui()
            return

        axis = get_axis_from_axes_stream(axes_stream, axis_id)

        if axis is None:
            self.status_text = f"Axis {axis_id!r} is not available on the selected display panel."
            self._notify_property_changed("status_text")
            self._refresh_axes_from_axes_stream(axes_stream)
            self._rebuild_ui()
            return

        graphics_data_item = self._get_active_data_item()

        if graphics_data_item is None:
            self.status_text = "No active API data item available for axis overlay."
            self._notify_property_changed("status_text")
            return

        data_item_key = self._get_data_item_key(graphics_data_item)
        overlay_key: AxisGraphicKey = (data_item_key, axis_id)

        if overlay_key in self._axis_graphics:
            self._remove_graphics_for_key(overlay_key)
            self.status_text = f"Removed axis {axis.display_name} from active data item."
            self._notify_property_changed("status_text")
            return

        safeid = self._axis_id_to_safeid.get(axis_id, self._sanitize_axis_id(axis_id))
        color = self.__dict__.get(f"axis_color_{safeid}", axis.color)

        if not isinstance(color, str):
            color = axis.color

        graphics = self._show_axis_overlay(data_item_key, axis, graphics_data_item, color)

        if graphics is None:
            return

        self._axis_graphics[overlay_key] = VisibleAxisOverlay(
            data_item_key=data_item_key,
            axis_id=axis_id,
            axis=axis,
            graphics_data_item=graphics_data_item,
            color=color,
            graphics=graphics
        )

        self.status_text = f"Displayed axis {axis.display_name} on active data item."
        self._notify_property_changed("status_text")

    def _remove_graphics_for_key(self, overlay_key: AxisGraphicKey) -> None:
        overlay = self._axis_graphics.pop(overlay_key, None)

        if overlay is None:
            return

        for data_item, graphic in overlay.graphics:
            try:
                data_item.remove_region(graphic)
            except Exception:
                traceback.print_exc()

    def _remove_all_axis_graphics(self) -> None:
        for overlay_key in list(self._axis_graphics.keys()):
            self._remove_graphics_for_key(overlay_key)

    def _rebuild_existing_axes(self) -> None:
        """Rebuild all currently visible overlays after display-option changes."""

        overlays = list(self._axis_graphics.values())
        self._axis_graphics.clear()

        for overlay in overlays:
            for data_item, graphic in overlay.graphics:
                try:
                    data_item.remove_region(graphic)
                except Exception:
                    traceback.print_exc()

        for overlay in overlays:
            graphics = self._show_axis_overlay(
                overlay.data_item_key,
                overlay.axis,
                overlay.graphics_data_item,
                overlay.color
            )

            if graphics is None:
                continue

            overlay_key: AxisGraphicKey = (overlay.data_item_key, overlay.axis_id)

            self._axis_graphics[overlay_key] = VisibleAxisOverlay(
                data_item_key=overlay.data_item_key,
                axis_id=overlay.axis_id,
                axis=overlay.axis,
                graphics_data_item=overlay.graphics_data_item,
                color=overlay.color,
                graphics=graphics
            )

    # Declarative UI expects on_<name> handlers for direct button callbacks.
    def on_clear_all_clicked(self, widget: Declarative.UIWidget) -> None:
        self._remove_all_axis_graphics()
        self.status_text = "Cleared all axis overlays."
        self._notify_property_changed("status_text")

    # Declarative UI expects on_<name> handlers for direct button callbacks.
    def on_refresh_clicked(self, widget: Declarative.UIWidget) -> None:
        self.refresh_from_current_selection()


# --------------------------------------------------------------------------------------
# Panel registration
# --------------------------------------------------------------------------------------

class ExperimentalAxesPlotterPanel(Panel.Panel):
    def __init__(self, document_controller: DocumentController.DocumentController, panel_id: str) -> None:
        """Create the experimental panel."""

        super().__init__(document_controller, panel_id, PANEL_TITLE)

        self.__document_controller = document_controller
        self.__display_item_changed_listeners: list[EventListenerLike] = []

        ui = document_controller.ui

        self.__content_column = ui.create_column_widget()

        self.widget = self.__content_column

        self.__handler = ExperimentalAxesPlotterHandler(
            rebuild_widget_fn=self.__rebuild_widget
        )

        self.__declarative_widget: Declarative.DeclarativeWidget | None = None

        self.__rebuild_widget()
        self.__connect_display_selection_listener()
        self.__load_initial_display_item()

    def __rebuild_widget(self) -> None:
        self.__content_column.remove_all()

        self.__declarative_widget = Declarative.DeclarativeWidget(
            self.__document_controller.ui,
            self.__document_controller.event_loop,
            self.__handler
        )

        self.__content_column.add(self.__declarative_widget)

    def __connect_display_selection_listener(self) -> None:
        focused_listener = self.__document_controller.focused_display_item_changed_event.listen(
            self.__display_item_changed
        )

        self.__display_item_changed_listeners.append(focused_listener)

    def __get_current_display_item(self) -> ApiDisplayLike | None:
        focused_display_item = self.__document_controller.focused_display_item

        if focused_display_item is not None:
            return typing.cast(ApiDisplayLike, focused_display_item)

        selected_display_item = self.__document_controller.selected_display_item

        if selected_display_item is not None:
            return typing.cast(ApiDisplayLike, selected_display_item)

        return None

    def __load_initial_display_item(self) -> None:
        self.__handler.set_display_item(self.__get_current_display_item())

    def __display_item_changed(self, display_item: object | None = None) -> None:
        current_display_item = self.__get_current_display_item()

        if current_display_item is not None:
            self.__handler.set_display_item(current_display_item)
            return

        self.__handler.set_display_item(typing.cast(ApiDisplayLike | None, display_item))

    def close(self) -> None:
        for listener in self.__display_item_changed_listeners:
            try:
                listener.close()
            except Exception:
                traceback.print_exc()

        self.__display_item_changed_listeners.clear()

        self.__handler.close_for_panel()
        super().close()


def register_panel() -> None:
    Workspace.WorkspaceManager().register_panel(
        ExperimentalAxesPlotterPanel,
        PANEL_ID,
        PANEL_TITLE,
        ["left", "right"],
        "right",
        {}
    )


def unregister_panel() -> None:
    Workspace.WorkspaceManager().unregister_panel(PANEL_ID)


class ExperimentalAxesPlotterExtension:
    extension_id = "nion.extension.experimental_axes_plotter"

    def __init__(self, api_broker: ApiBrokerLike) -> None:
        """Register the panel."""

        api = typing.cast(Facade.API, api_broker.get_api(version="~1.0"))
        typing.cast(ApiLike, api)

        register_panel()

    def close(self) -> None:
        unregister_panel()


# --------------------------------------------------------------------------------------
# Temporary stream construction - metadata navigation lives here for now
#
# This block should eventually be replaced by a real AxesStream supplied by
# metadata / niondata. Until then a temporary method for raw metadata navigation
# and parsing is intentionally collected here.
# --------------------------------------------------------------------------------------

@temporary(TEMPORARY_STREAM_REASON)
def _is_mapping(value: typing.Any) -> typing.TypeGuard[typing.Mapping[typing.Any, typing.Any]]:
    return isinstance(value, typing.Mapping)


@temporary(TEMPORARY_STREAM_REASON)
def _metadata_get_path(metadata: typing.Mapping[typing.Any, typing.Any], path: str) -> typing.Any:
    """Read a dotted metadata path from the known metadata dictionary shape."""

    if path in metadata:
        return metadata[path]

    current: typing.Any = metadata

    for part in path.split("."):
        if not _is_mapping(current):
            return None

        if part not in current:
            return None

        current = current[part]

    return current


@temporary(TEMPORARY_STREAM_REASON)
def get_data_item_metadata(data_item: ApiDataItemLike) -> typing.Mapping[typing.Any, typing.Any]:
    """Return metadata from the known data-item shape."""

    metadata = data_item.metadata

    if _is_mapping(metadata):
        return metadata

    return {}


@temporary(TEMPORARY_STREAM_REASON)
def get_axis_transformation_matrices_metadata(data_item: ApiDataItemLike) -> typing.Mapping[typing.Any, typing.Any] | None:
    """Return instrument axis transformation matrices from metadata."""

    axis_transformation_matrices = _metadata_get_path(
        get_data_item_metadata(data_item),
        AXIS_TRANSFORMATION_MATRICES_METADATA_PATH
    )

    if _is_mapping(axis_transformation_matrices):
        return axis_transformation_matrices

    return None


@temporary(TEMPORARY_STREAM_REASON)
def _read_float_point(value: typing.Any) -> Geometry.FloatPoint | None:
    """Read a point/vector from sequence, y/x mapping, or 0/1 mapping."""

    if isinstance(value, typing.Sequence) and not isinstance(value, str) and len(value) >= 2:
        try:
            return Geometry.FloatPoint(y=float(value[0]), x=float(value[1]))
        except (TypeError, ValueError):
            return None

    if _is_mapping(value):
        for y_key, x_key in (("y", "x"), (0, 1), ("0", "1")):
            if y_key in value and x_key in value:
                try:
                    return Geometry.FloatPoint(
                        y=float(value[y_key]),
                        x=float(value[x_key])
                    )
                except (TypeError, ValueError):
                    return None

    return None


@temporary(TEMPORARY_STREAM_REASON)
def _humanize_axis_id(axis_id: str) -> str:
    names = {
        "tv": "TV",
        "scan": "Scan",
        "gun": "Gun",
        "mc": "MC",
        "eels": "EELS",
        "correctoraxis": "Corrector Axis",
        "postsample": "Post Sample",
        "stageaxis": "Stage Axis",
        "stagetiltaxis": "Stage Tilt Axis"
    }

    normalized = axis_id.lower()

    if normalized in names:
        return names[normalized]

    return axis_id.replace("_", " ").replace("-", " ").title()


@temporary(TEMPORARY_STREAM_REASON)
def _default_axis_color(axis_id: str) -> str:
    normalized = "".join(ch.lower() for ch in axis_id if ch.isalnum())

    if normalized == "tv":
        return "#77FF1C"

    if normalized in {"correctoraxis", "corrector"}:
        return "#FC3A0F"

    if normalized in {"eels", "eelsaxis"}:
        return "#FF7B00"

    if normalized == "mc":
        return "#E64DFF"

    if normalized in {"postsample", "postsampleaxis", "post"}:
        return "#CCB1B1"

    if normalized == "scan":
        return "#FF0090"

    if normalized in {"stageaxis", "stage"}:
        return "#00F2FF"

    if normalized in {"stagetiltaxis", "stagetilt"}:
        return "#EEFF00"

    if normalized == "gun":
        return "#4DA3FF"

    return "#8E8E93"


@temporary(TEMPORARY_STREAM_REASON)
def _as_float(value: typing.Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@temporary(TEMPORARY_STREAM_REASON)
def _metadata_key_sort_value(key: typing.Any) -> tuple[int, int | str]:
    """Return a stable sort key for scalar metadata entries.

    Numeric-looking keys are sorted numerically so keys such as 0, 1, 2, 10
    are ordered correctly. Non-numeric keys fall back to string ordering.
    """

    if isinstance(key, int) and not isinstance(key, bool):
        return 0, key

    key_text = str(key)

    try:
        return 0, int(key_text)
    except ValueError:
        return 1, key_text


@temporary(TEMPORARY_STREAM_REASON)
def _read_matrix_from_mapping(value: typing.Mapping[typing.Any, typing.Any]) -> tuple[tuple[str, str], Geometry.FloatPoint, Geometry.FloatPoint] | None:
    """Read named basis vectors from one axis transformation matrix."""

    explicit_pairs: tuple[tuple[typing.Any, typing.Any], ...] = (
        ("x_vector", "y_vector"),
        ("x", "y"),
        ("X", "Y"),
        ("a", "b"),
        ("A", "B"),
        ("0", "1"),
        (0, 1)
    )

    for first_key, second_key in explicit_pairs:
        if first_key in value and second_key in value:
            first_vector = _read_float_point(value[first_key])
            second_vector = _read_float_point(value[second_key])

            if first_vector is not None and second_vector is not None:
                first_name = str(first_key)
                second_name = str(second_key)

                if first_name == "x_vector":
                    first_name = "x"

                if second_name == "y_vector":
                    second_name = "y"

                return (first_name, second_name), first_vector, second_vector

    vector_items: list[tuple[str, typing.Any]] = []

    for key, item_value in value.items():
        if isinstance(item_value, typing.Sequence) and not isinstance(item_value, str):
            vector_items.append((str(key), item_value))
        elif _is_mapping(item_value):
            vector_items.append((str(key), item_value))

    vectors: list[tuple[str, Geometry.FloatPoint]] = []

    for key, item_value in vector_items:
        point = _read_float_point(item_value)

        if point is not None:
            vectors.append((key, point))

    if len(vectors) >= 2:
        first_name, first_vector = vectors[0]
        second_name, second_vector = vectors[1]
        return (first_name, second_name), first_vector, second_vector

    scalar_values: list[float] = []

    for _key, item_value in sorted(value.items(), key=lambda item: _metadata_key_sort_value(item[0])):
        scalar = _as_float(item_value)

        if scalar is not None:
            scalar_values.append(scalar)

    if len(scalar_values) >= 4:
        return (
            ("x", "y"),
            Geometry.FloatPoint(y=scalar_values[0], x=scalar_values[1]),
            Geometry.FloatPoint(y=scalar_values[2], x=scalar_values[3])
        )

    return None


@temporary(TEMPORARY_STREAM_REASON)
def _read_matrix_axis_metadata(axis_id: str, value: typing.Any) -> CoordinateAxis | None:
    """Read one axis from instrument.axis_transformation_matrices metadata."""

    axis_type: tuple[str, str]
    x_vector: Geometry.FloatPoint
    y_vector: Geometry.FloatPoint

    if _is_mapping(value):
        read_result = _read_matrix_from_mapping(value)

        if read_result is None:
            return None

        axis_type, x_vector, y_vector = read_result

    elif isinstance(value, typing.Sequence) and not isinstance(value, str):
        first_vector: Geometry.FloatPoint | None = None
        second_vector: Geometry.FloatPoint | None = None

        if len(value) >= 2:
            first_vector = _read_float_point(value[0])
            second_vector = _read_float_point(value[1])

        if first_vector is not None and second_vector is not None:
            axis_type = ("x", "y")
            x_vector = first_vector
            y_vector = second_vector

        elif len(value) >= 4:
            scalar_values = [_as_float(item) for item in value[:4]]

            if not all(item is not None for item in scalar_values):
                return None

            axis_type = ("x", "y")
            x_vector = Geometry.FloatPoint(
                y=typing.cast(float, scalar_values[0]),
                x=typing.cast(float, scalar_values[1])
            )
            y_vector = Geometry.FloatPoint(
                y=typing.cast(float, scalar_values[2]),
                x=typing.cast(float, scalar_values[3])
            )

        else:
            return None

    else:
        return None

    return CoordinateAxis(
        axis_id=axis_id,
        display_name=_humanize_axis_id(axis_id),
        axis_type=axis_type,
        origin=Geometry.FloatPoint(y=0.5, x=0.5),
        x_vector=x_vector,
        y_vector=y_vector,
        color=_default_axis_color(axis_id),
        metadata_source=AXIS_TRANSFORMATION_MATRICES_METADATA_PATH
    )


@temporary(TEMPORARY_STREAM_REASON)
def get_axes_from_axis_transformation_matrices(data_item: ApiDataItemLike) -> typing.Mapping[str, CoordinateAxis]:
    """Return axes from instrument.axis_transformation_matrices metadata."""

    axis_transformation_matrices = get_axis_transformation_matrices_metadata(data_item)

    if axis_transformation_matrices is None:
        return types.MappingProxyType({})

    axes: dict[str, CoordinateAxis] = {}

    for raw_axis_id, axis_metadata in axis_transformation_matrices.items():
        axis_id = str(raw_axis_id)
        axis = _read_matrix_axis_metadata(axis_id, axis_metadata)

        if axis is not None:
            axes[axis.axis_id] = axis

    return types.MappingProxyType(axes)


@temporary(TEMPORARY_STREAM_REASON)
def get_coordinate_system_axes(data_item: ApiDataItemLike) -> typing.Mapping[str, CoordinateAxis]:
    """Return available coordinate axes from the current metadata format."""

    return get_axes_from_axis_transformation_matrices(data_item)


@temporary(TEMPORARY_STREAM_REASON)
def get_coordinate_metadata_source_summary(data_item: ApiDataItemLike) -> str:
    axis_transformation_matrices = get_axis_transformation_matrices_metadata(data_item)

    if axis_transformation_matrices is not None:
        axes = get_axes_from_axis_transformation_matrices(data_item)

        if axes:
            return AXIS_TRANSFORMATION_MATRICES_METADATA_PATH

    return "none"


@temporary(TEMPORARY_STREAM_REASON)
def create_axes_stream_from_data_item(data_item: ApiDataItemLike) -> AxesStream | None:
    """Construct an AxesStream from the known data-item metadata shape."""

    axes = get_coordinate_system_axes(data_item)

    if not axes:
        return None

    return AxesStream(
        source_data_item=data_item,
        axes=types.MappingProxyType(dict(axes)),
        metadata_source=get_coordinate_metadata_source_summary(data_item)
    )


@temporary(TEMPORARY_STREAM_REASON)
def create_axes_stream_from_display_item(display_item: ApiDisplayLike) -> AxesStream | None:
    """Construct an AxesStream from the known display-item shape."""

    data_item = display_item.data_item

    if data_item is None:
        return None

    return create_axes_stream_from_data_item(data_item)
