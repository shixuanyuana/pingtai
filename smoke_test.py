from app.main import (
    import_match_templates,
    import_order_templates,
    import_reservation_templates,
    index,
    init_db,
    list_cases,
    list_environments,
    create_live_scenario_definition,
    create_recording_scenario_definition,
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
    scenario = create_live_scenario_definition()
    assert [step["path"] for step in scenario["steps"]] == [
        "/develop/reservation-orders", "/develop/matches", "/develop/matches"
    ]
    assert scenario["steps"][1]["association"] == "server_sn ← 创建预约订单.data.auth_code"
    assert {field["name"] for field in scenario["fields"]} >= {
        "store_id", "start_time", "end_time", "voucher_code", "matches_name"
    }
    recording = create_recording_scenario_definition()
    assert [step["path"] for step in recording["steps"]] == [
        "/develop/recordings", "/develop/recordings?page_num=1&page_size=20&type=video"
    ]
    assert {field["name"] for field in recording["fields"]} >= {
        "recording_name", "store_id", "start_time", "end_time", "voucher_code"
    }
    print("smoke-test-ok: 3 categories, 2 scenario definitions, web page available")


if __name__ == "__main__":
    main()
