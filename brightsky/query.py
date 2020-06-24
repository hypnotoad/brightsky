import datetime

from brightsky.db import fetch


def _make_dicts(rows):
    return [dict(row) for row in rows]


def weather(
        date, last_date=None, lat=None, lon=None, dwd_station_id=None,
        wmo_station_id=None, source_id=None, max_dist=50000, fallback=True):
    if not last_date:
        last_date = date + datetime.timedelta(days=1)
    if source_id is not None:
        weather = _weather(date, last_date, source_id)
        if not weather:
            # Make sure we throw a LookupError if the source id does not exist
            sources(source_id=source_id)
        return {'weather': weather}
    else:
        sources_rows = sources(
            lat=lat, lon=lon, dwd_station_id=dwd_station_id,
            wmo_station_id=wmo_station_id, max_dist=max_dist
        )['sources']
        source_ids = [row['id'] for row in sources_rows]
        weather_rows = _weather(date, last_date, source_ids)
        if fallback:
            _fill_missing_fields(weather_rows, date, last_date, source_ids)
        used_source_ids = {row['source_id'] for row in weather_rows}
        used_source_ids.update(
            source_id
            for row in weather_rows
            for source_id in row.get('fallback_source_ids', {}).values())
        return {
            'weather': weather_rows,
            'sources': [s for s in sources_rows if s['id'] in used_source_ids],
        }


def _weather(date, last_date, source_id, not_null=None):
    params = {
        'date': date,
        'last_date': last_date,
        'source_id': source_id,
    }
    where = "timestamp BETWEEN %(date)s AND %(last_date)s"
    order_by = "timestamp"
    if isinstance(source_id, list):
        where += " AND source_id IN %(source_id_tuple)s"
        order_by += ", array_position(%(source_id)s, source_id)"
        params['source_id_tuple'] = tuple(source_id)
    else:
        where += " AND source_id = %(source_id)s"
    if not_null:
        where += ''.join(f" AND {element} IS NOT NULL" for element in not_null)
    sql = f"""
        SELECT DISTINCT ON (timestamp) *
        FROM weather
        WHERE {where}
        ORDER BY {order_by}
    """
    return _make_dicts(fetch(sql, params))


def _fill_missing_fields(weather_rows, date, last_date, source_ids):
    incomplete_rows = []
    missing_fields = set()
    for row in weather_rows:
        missing_row_fields = set(k for k, v in row.items() if v is None)
        if missing_row_fields:
            incomplete_rows.append((row, missing_row_fields))
            missing_fields.update(missing_row_fields)
    if incomplete_rows:
        min_date = incomplete_rows[0][0]['timestamp']
        max_date = incomplete_rows[-1][0]['timestamp']
        # NOTE: If there are multiple missing fields we may be missing out on
        #       a "better" fallback if there are preferred sources that have
        #       one (but not all) of the missing fields. However, this lets us
        #       get by with using the basic weather query, and with performing
        #       it only one extra time.
        fallback_rows = {
            row['timestamp']: row
            for row in _weather(
                min_date, max_date, source_ids, not_null=missing_fields)
        }
        for row, fields in incomplete_rows:
            fallback_row = fallback_rows.get(row['timestamp'])
            if fallback_row:
                row['fallback_source_ids'] = {}
                for f in fields:
                    row[f] = fallback_row[f]
                    row['fallback_source_ids'][f] = fallback_row['source_id']


def sources(
        lat=None, lon=None, dwd_station_id=None, wmo_station_id=None,
        source_id=None, max_dist=50000, ignore_type=False):
    select = """
        id,
        dwd_station_id,
        wmo_station_id,
        station_name,
        observation_type,
        lat,
        lon,
        height
    """
    order_by = "observation_type"
    if source_id is not None:
        where = "id = %(source_id)s"
    elif dwd_station_id is not None:
        where = "dwd_station_id = %(dwd_station_id)s"
    elif wmo_station_id is not None:
        where = "wmo_station_id = %(wmo_station_id)s"
    elif (lat is not None and lon is not None):
        distance = """
            earth_distance(
                ll_to_earth(%(lat)s, %(lon)s),
                ll_to_earth(lat, lon)
            )
        """
        select += f", {distance} AS distance"
        where = f"""
            earth_box(
                ll_to_earth(%(lat)s, %(lon)s),
                %(max_dist)s
            ) @> ll_to_earth(lat, lon) AND
            {distance} < %(max_dist)s
        """
        if ignore_type:
            order_by = "distance"
        else:
            order_by += ", distance"
    else:
        raise ValueError(
            "Please supply lat/lon or dwd_station_id or wmo_station_id or "
            "source_id")
    sql = f"""
        SELECT {select}
        FROM sources
        WHERE {where}
        ORDER BY {order_by}
        """
    params = {
        'lat': lat,
        'lon': lon,
        'max_dist': max_dist,
        'dwd_station_id': dwd_station_id,
        'wmo_station_id': wmo_station_id,
        'source_id': source_id,
    }
    rows = fetch(sql, params)
    if not rows:
        raise LookupError("No sources match your criteria")
    return {'sources': _make_dicts(rows)}
