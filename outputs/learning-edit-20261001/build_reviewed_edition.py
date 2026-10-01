"""Export the locally edited edition without claiming external API review passed."""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cards
import learning_editor as editor


def main():
    directory = Path(__file__).resolve().parent
    content = json.loads((directory / "reviewed_content.json").read_text(encoding="utf-8"))
    source_path = directory.parents[1] / content["source"]
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == "90e6876a0188743498304fe3a8b400f1827b2e70f8eea00f8a38f4281e78c50a"
    source = cards.load_xlsx_items(str(source_path))
    groups, items = [], []
    for entry in content["groups"]:
        group = {"core": entry["core"], "source_ids": entry["source_ids"],
                 "scenario": editor.SCENARIOS[entry["scenario_id"] - 1], "boundary": entry["boundary"]}
        groups.append(group)
        for card in entry["cards"]:
            item = dict(card, id=f"{len(items) + 1:02d}", core=group["core"], Scenario=group["scenario"],
                        Level=editor.LEVELS[card["tier"]], tips=card["Tone"] + "：" + card["tips"])
            item["Core_Vocab"] = "；".join(v["en"] + " " + v["cn"] for v in item["vocab"])
            items.append(item)
    editor.validate_deck(items, groups, source)
    assert len(groups) == 16 and len(items) == 32
    assert set(Counter(item["Scenario"] for item in items).values()) == {8}
    state = {"source": source, "groups": groups, "items": items,
             "editorial_method": content["editor"], "external_api_review": content["external_api_review"]}
    target = directory / "推銷與隱形敲詐_精簡學習版.xlsx"
    if target.exists() and "--replace-edition" not in sys.argv:
        raise ValueError("修訂版已存在；拒絕意外覆蓋。")
    assert target != source_path
    cards.write_xlsx(items, str(target), learning=True)
    Path(str(target) + ".editor.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(target), "original_rows": len(source), "concepts": len(groups),
                      "rows": len(items), "scenarios": dict(Counter(item["Scenario"] for item in items)),
                      "external_api_review": state["external_api_review"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
