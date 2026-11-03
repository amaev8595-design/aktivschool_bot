import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SchoolClass:
    name: str
    age_group: str
    invite_code: str


def load_classes() -> tuple[SchoolClass, ...]:
    path = Path(__file__).parent / "data" / "classes.csv"
    with path.open(encoding="utf-8", newline="") as file:
        return tuple(
            SchoolClass(
                name=row["name"].strip(),
                age_group=row["age_group"].strip(),
                invite_code=row["invite_code"].strip(),
            )
            for row in csv.DictReader(file)
            if row.get("name")
        )


def format_rating(classes: tuple[SchoolClass, ...]) -> str:
    groups = {"1-4": [], "5-8": [], "9-11": []}
    for school_class in classes:
        groups.setdefault(school_class.age_group, []).append(school_class.name)

    lines = ["🏆 Демонстрационный рейтинг", "Баллы пока равны нулю."]
    for age_group, names in groups.items():
        if names:
            lines.append(f"\nГруппа {age_group}:\n" + "\n".join(f"• {name} — 0 баллов" for name in names))
    return "\n".join(lines)


SCHOOL_CLASSES = load_classes()

