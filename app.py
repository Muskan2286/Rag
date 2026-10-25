from __future__ import annotations

import csv
import html
import itertools
import math
import os
import time
from datetime import time as datetime_time
from io import StringIO
from typing import Any

import folium
import requests
import streamlit as st
from streamlit_folium import st_folium


st.set_page_config(
    page_title="Local Delivery Route Planner",
    page_icon="D",
    layout="wide",
)

st.markdown(
    """
    <style>
    :root {
        --ink: #172525;
        --muted: #5d6c68;
        --line: #dce5df;
        --green: #176b52;
        --green-soft: #eaf3ed;
        --coral: #c85e42;
        --paper: #f6f8f4;
    }
    .stApp { background: var(--paper); color: var(--ink); }
    .block-container { max-width: 1280px; padding-top: 2rem; }
    h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
    h1 { font-family: Georgia, 'Times New Roman', serif; font-weight: 600; }
    [data-testid="stMetric"] {
        background: #fff; border: 1px solid var(--line); padding: 14px 16px;
        border-radius: 6px;
    }
    [data-testid="stForm"] {
        background: #fff; border: 1px solid var(--line); padding: 1rem 1.2rem;
        border-radius: 6px;
    }
    .eyebrow { color: var(--green); font-size: .78rem; font-weight: 700;
        letter-spacing: .08em; text-transform: uppercase; }
    .subtle { color: var(--muted); }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_default_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    try:
        key = key or st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        pass
    return key


def geocode(address: str, area: str) -> dict[str, Any]:
    query = f"{address}, {area}" if area else address
    response = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": "LocalDeliveryRoutePlanner/1.0 (student project)"},
        timeout=20,
    )
    response.raise_for_status()
    matches = response.json()
    if not matches:
        raise ValueError(f"Could not find this address: {address}")
    match = matches[0]
    return {
        "lat": float(match["lat"]),
        "lon": float(match["lon"]),
        "label": match.get("display_name", address),
    }


def haversine_meters(first: dict[str, Any], second: dict[str, Any]) -> float:
    earth_radius = 6_371_000
    lat1, lat2 = math.radians(first["lat"]), math.radians(second["lat"])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(second["lon"] - first["lon"])
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius * math.asin(math.sqrt(value))


def route_plan(
    points: list[dict[str, Any]], has_depot: bool, preference: str
) -> tuple[list[int], float, float, list[list[float]] | None, bool, bool]:
    coordinates = ";".join(f"{point['lon']},{point['lat']}" for point in points)
    matrix_url = f"https://router.project-osrm.org/table/v1/driving/{coordinates}"
    used_fallback = False
    try:
        response = requests.get(
            matrix_url,
            params={"annotations": "distance,duration"},
            timeout=25,
        )
        response.raise_for_status()
        matrix_result = response.json()
        if matrix_result.get("code") != "Ok":
            raise ValueError("The routing service did not return a route matrix.")
        distances = matrix_result["distances"]
        durations = matrix_result["durations"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        used_fallback = True
        distances = [
            [
                0.0 if first == second else haversine_meters(points[first], points[second]) * 1.3
                for second in range(len(points))
            ]
            for first in range(len(points))
        ]
        durations = [[distance / 8.33 for distance in row] for row in distances]

    score_matrix = durations if preference == "Fastest" else distances
    if has_depot:
        candidates = (
            (0, *order)
            for order in itertools.permutations(range(1, len(points)))
        )
    else:
        candidates = itertools.permutations(range(len(points)))

    order = min(
        candidates,
        key=lambda candidate: sum(
            score_matrix[candidate[index]][candidate[index + 1]]
            for index in range(len(candidate) - 1)
        ),
    )
    total_distance = sum(
        distances[order[index]][order[index + 1]]
        for index in range(len(order) - 1)
    )
    total_duration = sum(
        durations[order[index]][order[index + 1]]
        for index in range(len(order) - 1)
    )

    route_geometry = None
    route_fallback = False
    ordered_coordinates = ";".join(
        f"{points[index]['lon']},{points[index]['lat']}" for index in order
    )
    try:
        route_response = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{ordered_coordinates}",
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            timeout=25,
        )
        route_response.raise_for_status()
        route_data = route_response.json()
        if route_data.get("code") == "Ok" and route_data.get("routes"):
            route_geometry = route_data["routes"][0]["geometry"]["coordinates"]
        else:
            route_fallback = True
    except (requests.RequestException, ValueError, KeyError, TypeError):
        route_fallback = True
    return list(order), total_distance, total_duration, route_geometry, used_fallback, route_fallback


def local_delivery_notes(
    order: list[int], points: list[dict[str, Any]], departure: datetime_time, weather: str
) -> str:
    rush_hour = departure.hour in range(7, 10) or departure.hour in range(16, 19)
    lines = ["### Suggested dispatch sequence"]
    for position, point_index in enumerate(order):
        point = points[point_index]
        label = "Depot" if point.get("kind") == "depot" else f"Stop {point['stop_id']}"
        details = []
        if point.get("parking"):
            details.append(f"Parking: {point['parking']}")
        if point.get("parcel"):
            details.append(f"Parcel: {point['parcel']}")
        lines.append(f"{position + 1}. **{label}:** {point['address']}")
        if details:
            lines.append(f"   {' | '.join(details)}")

    lines.extend(["", "### Driver reminders"])
    if rush_hour:
        lines.append(
            f"- Departure at {departure.strftime('%H:%M')} falls in a typical rush-hour window; "
            "allow extra time and follow posted loading and traffic restrictions."
        )
    else:
        lines.append(
            "- Check current traffic before departure; this prototype does not include live traffic. "
            "Follow posted road and loading restrictions."
        )
    lines.append(
        "- Secure heavy parcels low and close to the vehicle floor, balance the load, and use a "
        "trolley or team lift when appropriate."
    )
    lines.append(
        "- Reduce fuel use by grouping nearby deliveries, avoiding unnecessary idling, and "
        "driving smoothly. The suggested route is approximate."
    )
    if weather:
        lines.append(f"- Weather note supplied by the planner: {weather}")
    else:
        lines.append("- Add a local weather note before dispatch; live weather is not connected.")
    return "\n".join(lines)


def gemini_delivery_notes(
    api_key: str,
    order: list[int],
    points: list[dict[str, Any]],
    departure: datetime_time,
    weather: str,
) -> str:
    sequence = []
    for position, point_index in enumerate(order):
        point = points[point_index]
        sequence.append(
            f"{position + 1}. {point.get('kind', 'stop').title()} - {point['address']}; "
            f"parking: {point.get('parking') or 'not provided'}; "
            f"parcel: {point.get('parcel') or 'not provided'}"
        )
    prompt = (
        "You are a practical local delivery dispatch assistant. Use only the supplied facts. "
        "Write concise driver instructions for this proposed dispatch order. Mention likely "
        "rush-hour caution based on the departure time, each supplied parking note, heavy-parcel "
        "handling, and fuel-saving practices. Do not invent traffic laws, parking availability, "
        "weather, or live traffic. Clearly say live traffic and weather have not been checked. "
        "Weather note supplied by planner: " + (weather or "none") + "\n"
        f"Departure time: {departure.strftime('%H:%M')} (local time entered by planner).\n"
        "Proposed sequence:\n" + "\n".join(sequence)
    )
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=45,
    )
    response.raise_for_status()
    result = response.json()
    return result["candidates"][0]["content"]["parts"][0]["text"]


def make_map(
    points: list[dict[str, Any]], order: list[int], geometry: list[list[float]] | None
) -> folium.Map:
    center = [
        sum(point["lat"] for point in points) / len(points),
        sum(point["lon"] for point in points) / len(points),
    ]
    route_map = folium.Map(location=center, zoom_start=13, tiles="OpenStreetMap")
    if geometry:
        line = [(coordinate[1], coordinate[0]) for coordinate in geometry]
    else:
        line = [(points[index]["lat"], points[index]["lon"]) for index in order]
    folium.PolyLine(line, color="#176b52", weight=5, opacity=0.85).add_to(route_map)

    for sequence_number, point_index in enumerate(order, start=1):
        point = points[point_index]
        marker_label = "D" if point.get("kind") == "depot" else str(point["stop_id"])
        popup_text = html.escape(point["address"])
        popup = folium.Popup(f"{marker_label}. {popup_text}", max_width=300)
        folium.Marker(
            [point["lat"], point["lon"]],
            popup=popup,
            tooltip=f"Route {sequence_number}: {marker_label}",
            icon=folium.DivIcon(
                html=(
                    '<div style="background:#176b52;color:#fff;border:2px solid #fff;'
                    'border-radius:50%;width:30px;height:30px;line-height:26px;'
                    'text-align:center;font-weight:700;box-shadow:0 1px 5px #555">'
                    f"{html.escape(marker_label)}</div>"
                )
            ),
        ).add_to(route_map)

    bounds = [[point["lat"], point["lon"]] for point in points]
    route_map.fit_bounds(bounds, padding=(28, 28))
    return route_map


def make_route_csv(points: list[dict[str, Any]], order: list[int]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["sequence", "stop", "address", "parking_note", "parcel_note", "latitude", "longitude"]
    )

    def safe_cell(value: Any) -> Any:
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    for sequence_number, point_index in enumerate(order, start=1):
        point = points[point_index]
        label = "Depot" if point.get("kind") == "depot" else f"Stop {point['stop_id']}"
        writer.writerow(
            [
                sequence_number,
                label,
                safe_cell(point["address"]),
                safe_cell(point.get("parking", "")),
                safe_cell(point.get("parcel", "")),
                point["lat"],
                point["lon"],
            ]
        )
    return output.getvalue()


st.markdown('<div class="eyebrow">Logistics · Local delivery planning</div>', unsafe_allow_html=True)
st.title("Delivery Route Planner")
st.markdown(
    '<p class="subtle">Plan five local deliveries, inspect an approximate route, and prepare a driver brief.</p>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.subheader("Trip settings")
    area = st.text_input("City or service area", placeholder="e.g. Leeds, UK")
    depot_address = st.text_input(
        "Starting depot (optional)", placeholder="Leave blank to start at a delivery stop"
    )
    departure = st.time_input("Planned departure", value=datetime_time(8, 30))
    preference = st.radio("Route objective", ["Shortest", "Fastest"], horizontal=True)
    weather_note = st.text_area(
        "Weather or road caution", placeholder="e.g. Heavy rain expected; take extra care"
    )
    api_key = st.text_input(
        "Gemini API key (optional)",
        value=get_default_api_key(),
        type="password",
        help="Without a key, the app creates a useful local driver brief instead.",
    )
    st.caption("Map and geocoding use OpenStreetMap. Route estimates use the public OSRM demo service.")
    with st.expander("Ideas for the next version"):
        st.markdown(
            "- **Time windows and service times:** plan around customer availability and unloading duration.\n"
            "- **Live traffic and weather:** use a licensed provider to refresh ETAs and identify disruptions.\n"
            "- **Fleet planning:** assign stops across drivers while respecting vehicle and parcel capacity.\n"
            "- **Customer updates:** generate arrival windows and delivery-status notifications.\n"
            "- **Accessibility and sustainability:** flag mobility/access notes and compare lower-emission routes."
        )

with st.form("delivery_stops"):
    st.subheader("Delivery stops")
    st.caption("Enter five complete addresses. Add the city above to help locate them accurately.")
    stop_inputs: list[tuple[str, str, str]] = []
    for index in range(1, 6):
        address_column, parking_column, parcel_column = st.columns([4, 3, 3])
        with address_column:
            address = st.text_input(f"Stop {index} address", key=f"address_{index}")
        with parking_column:
            parking = st.text_input(
                f"Stop {index} parking note", key=f"parking_{index}", placeholder="Optional"
            )
        with parcel_column:
            parcel = st.text_input(
                f"Stop {index} parcel note", key=f"parcel_{index}", placeholder="Optional"
            )
        stop_inputs.append((address, parking, parcel))
    submitted = st.form_submit_button("Plan delivery route", type="primary", use_container_width=True)

if submitted:
    missing = [str(index) for index, (address, _, _) in enumerate(stop_inputs, start=1) if not address.strip()]
    if missing:
        st.error("Please enter an address for every stop. Missing stop(s): " + ", ".join(missing))
    elif not area.strip():
        st.error("Enter a city or service area so the addresses can be located accurately.")
    else:
        try:
            with st.spinner("Finding addresses and planning your route..."):
                points: list[dict[str, Any]] = []
                if depot_address.strip():
                    depot = geocode(depot_address.strip(), area.strip())
                    points.append({"kind": "depot", "address": depot_address.strip(), **depot})

                geocode_cache: dict[str, dict[str, Any]] = {}
                for index, (address, parking, parcel) in enumerate(stop_inputs, start=1):
                    normalized = address.strip()
                    cache_key = f"{normalized.casefold()}|{area.strip().casefold()}"
                    if cache_key not in geocode_cache:
                        geocode_cache[cache_key] = geocode(normalized, area.strip())
                        time.sleep(1.05)
                    points.append(
                        {
                            "kind": "stop",
                            "stop_id": index,
                            "address": normalized,
                            "parking": parking.strip(),
                            "parcel": parcel.strip(),
                            **geocode_cache[cache_key],
                        }
                    )

                has_depot = bool(depot_address.strip())
                order, distance, duration, geometry, matrix_fallback, route_fallback = route_plan(
                    points, has_depot, preference
                )
                notes = local_delivery_notes(order, points, departure, weather_note.strip())
                ai_failed = False
                if api_key.strip():
                    try:
                        notes = gemini_delivery_notes(
                            api_key.strip(), order, points, departure, weather_note.strip()
                        )
                    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
                        ai_failed = True
                st.session_state["planner_result"] = {
                    "points": points,
                    "order": order,
                    "distance": distance,
                    "duration": duration,
                    "geometry": geometry,
                    "matrix_fallback": matrix_fallback,
                    "route_fallback": route_fallback,
                    "notes": notes,
                    "ai_failed": ai_failed,
                }
        except (requests.RequestException, ValueError, KeyError) as error:
            st.error(f"Could not plan this trip: {error}")

result = st.session_state.get("planner_result")
if result:
    st.divider()
    if result["matrix_fallback"]:
        st.warning("Road routing was unavailable. The stop order and distance use a straight-line estimate.")
    elif result["route_fallback"]:
        st.warning("The road route line was unavailable. The map connects stops with straight lines.")
    if result["ai_failed"]:
        st.warning("Gemini could not generate a brief; showing the built-in driver instructions instead.")

    distance_km = result["distance"] / 1000
    minutes = result["duration"] / 60
    hours, remaining_minutes = divmod(round(minutes), 60)
    duration_label = f"{hours} hr {remaining_minutes} min" if hours else f"{remaining_minutes} min"
    metric_columns = st.columns(3)
    metric_columns[0].metric("Estimated driving distance", f"{distance_km:.1f} km")
    metric_columns[1].metric("Estimated driving time", duration_label)
    metric_columns[2].metric("Stops planned", "5")

    route_labels = []
    for point_index in result["order"]:
        point = result["points"][point_index]
        route_labels.append("Depot" if point.get("kind") == "depot" else f"Stop {point['stop_id']}")
    st.markdown("**Dispatch order:** " + " → ".join(route_labels))

    map_column, notes_column = st.columns([1.25, 0.75], gap="large")
    with map_column:
        st.subheader("Route map")
        st_folium(
            make_map(result["points"], result["order"], result["geometry"]),
            height=520,
            use_container_width=True,
            returned_objects=[],
        )
    with notes_column:
        st.subheader("Driver brief")
        st.caption("AI-generated with Gemini" if api_key.strip() and not result["ai_failed"] else "Built-in planning guidance")
        st.markdown(result["notes"])

    csv_data = make_route_csv(result["points"], result["order"])
    download_column, brief_column = st.columns(2)
    download_column.download_button(
        "Download itinerary CSV",
        data=csv_data,
        file_name="delivery-itinerary.csv",
        mime="text/csv",
        use_container_width=True,
    )
    brief_column.download_button(
        "Download driver brief",
        data=result["notes"],
        file_name="driver-brief.md",
        mime="text/markdown",
        use_container_width=True,
    )

    st.caption(
        "Planning aid only: route times are estimates and exclude live congestion, stops, and parking delays. "
        "Confirm addresses, road restrictions, weather, and safe loading practices before dispatch."
    )
else:
    st.info("Add your service area and five stops, then choose **Plan delivery route** to see the map and driver brief.")