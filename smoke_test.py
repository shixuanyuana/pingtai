from app.main import (
    import_match_templates,
    import_order_templates,
    import_reservation_templates,
    index,
    init_db,
    list_cases,
    list_environments,
)


def main() -> None:
    init_db()
    assert len(import_reservation_templates()) == 7
    assert len(import_match_templates()) == 6
    assert len(import_order_templates()) == 6
    assert str(index().path).endswith(("web\\index.html", "web/index.html"))
    assert {item["kind"] for item in list_environments()} >= {"test", "prod"}
    cases = list_cases(module="直播管理")
    counts = {
        category: len([item for item in cases if item["category"] == category])
        for category in ("直播预约", "直播列表", "直播订单")
    }
    assert counts["直播预约"] >= 7
    assert counts["直播列表"] >= 6
    assert counts["直播订单"] >= 6
    print("smoke-test-ok: 3 categories, 19 API cases, web page available")


if __name__ == "__main__":
    main()
