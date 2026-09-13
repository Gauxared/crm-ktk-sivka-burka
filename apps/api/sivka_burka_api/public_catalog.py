"""Read-only projection of the public catalogue."""

from sqlalchemy import text


class PublicCatalogUnavailable(Exception):
    pass


class PublicCatalogRuntime:
    def __init__(self, engine):
        if engine is None:
            raise ValueError("engine is required")
        self.engine = engine

    def read(self) -> dict[str, object]:
        with self.engine.connect() as connection:
            settings = connection.execute(text("""
                SELECT catalog_version, timezone, contact_info, location_link, visit_rules
                FROM club_settings WHERE id = 1
            """)).mappings().first()
            if settings is None or not settings["timezone"] or not settings["location_link"] or not settings["visit_rules"]:
                raise PublicCatalogUnavailable
            rows = connection.execute(text("""
                SELECT s.id AS service_id, s.title, s.description, s.information,
                       so.id AS option_id, so.duration_minutes, so.pricing_mode,
                       so.price_minor, so.currency
                FROM services s
                JOIN service_options so ON so.service_id = s.id
                WHERE s.active = true AND so.active = true
                ORDER BY s.sort_order, s.id, so.id
            """)).mappings().all()
        if not rows:
            raise PublicCatalogUnavailable
        services: list[dict[str, object]] = []
        by_id: dict[object, dict[str, object]] = {}
        for row in rows:
            service = by_id.get(row["service_id"])
            if service is None:
                service = {"id": str(row["service_id"]), "title": row["title"], "description": row["description"], "information": row["information"], "options": []}
                by_id[row["service_id"]] = service
                services.append(service)
            service["options"].append({"id": str(row["option_id"]), "duration_minutes": row["duration_minutes"], "pricing_mode": row["pricing_mode"], "price_minor": row["price_minor"], "currency": row["currency"]})
        return {"catalog_version": settings["catalog_version"], "club_timezone": settings["timezone"], "contact_info": settings["contact_info"], "location_link": settings["location_link"], "visit_rules": settings["visit_rules"], "services": services}
