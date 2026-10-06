import unittest

from mapwright.actions import (
    CombineColumns,
    Deduplicate,
    DropRows,
    MapColumn,
    MapValues,
    MoveValue,
    NormalizePhone,
    NormalizeText,
    ParseDate,
    ParseNumber,
    SetNull,
    Workspace,
    action_from_dict,
)
from mapwright.ingest import parse_text


def workspace(text, fields):
    source = parse_text(text)
    ws = Workspace(source, fields)
    for f in fields:
        if f in source.header:
            ws.commit(MapColumn(source=f, target=f).compute(ws))
    return ws


class ActionTest(unittest.TestCase):
    def test_combine_columns(self):
        ws = workspace("first,last\nSara,Ali\nNoura,\n", ["full_name"])
        ws.commit(CombineColumns(sources=["first", "last"], target="full_name").compute(ws))
        self.assertEqual(ws.values("full_name"), {1: "Sara Ali", 2: "Noura"})

    def test_normalize_text_reports_only_changed_cells(self):
        ws = workspace("name\n RANA  ALI \nSara Ali\n", ["name"])
        effect = NormalizeText(target="name", case="title_if_uniform").compute(ws)
        self.assertEqual([(c.row, c.after, c.issue) for c in effect.changes], [(1, "Rana Ali", "S4")])

    def test_set_null_by_token_and_by_row(self):
        ws = workspace("email\nN/A\nx@\nok@a.co\n", ["email"])
        self.assertEqual(SetNull(target="email", tokens=["n/a"]).compute(ws).rows(), [1])
        self.assertEqual(SetNull(target="email", rows=[2], issue="S6").compute(ws).rows(), [2])

    def test_map_values_ignores_case_and_spacing(self):
        ws = workspace("country\n ksa \nUAE\nMars\n", ["country"])
        effect = MapValues(target="country", mapping={"ksa": "SA", "uae": "AE"}, issue="V1").compute(ws)
        self.assertEqual({c.row: c.after for c in effect.changes}, {1: "SA", 2: "AE"})

    def test_parse_date_and_number(self):
        ws = workspace('d,amount,currency\n05/17/2023,"SAR 1,000.00",SAR\n,12.5K,USD\n', ["d", "amount", "currency"])
        dates = ParseDate(target="d", formats=["%m/%d/%Y"]).compute(ws)
        self.assertEqual([c.after for c in dates.changes], ["2023-05-17"])
        numbers = ParseNumber(target="amount", units={"SAR": "SAR"}, currency_field="currency").compute(ws)
        self.assertEqual([(c.after, c.issue) for c in numbers.changes], [("1000.00", "V4"), ("12500.00", "V4")])

    def test_parse_number_leaves_conflicting_currency(self):
        ws = workspace("amount,currency\nSAR 10,USD\n", ["amount", "currency"])
        effect = ParseNumber(target="amount", units={"SAR": "SAR"}, currency_field="currency").compute(ws)
        self.assertEqual(effect.changes, [])

    def test_normalize_phone(self):
        ws = workspace("phone\n0501234567\n123\n", ["phone"])
        effect = NormalizePhone(target="phone").compute(ws)
        self.assertEqual([(c.row, c.after) for c in effect.changes], [(1, "+966501234567")])

    def test_move_value_only_into_empty_cells(self):
        ws = workspace("email,phone\n0501234567,\n0509999999,+966500000000\n", ["email", "phone"])
        effect = MoveValue(source_field="email", target="phone", rows=[1, 2]).compute(ws)
        ws.commit(effect)
        self.assertEqual(ws.rows[1], {"email": "", "phone": "0501234567"})
        self.assertEqual(ws.rows[2]["email"], "0509999999")

    def test_row_actions(self):
        ws = workspace("id\nA\nA\nB\nC\n", ["id"])
        self.assertEqual(Deduplicate(keys=["id"], groups=[[1, 2]]).compute(ws).removed, [2])
        effect = DropRows(target="id", rows=[4, 9]).compute(ws)
        ws.commit(effect)
        self.assertEqual(effect.removed, [4])
        self.assertEqual(list(ws.rows), [1, 2, 3])

    def test_round_trip_through_json_form(self):
        actions = [
            ParseDate(id="a3", target="d", formats=["%d/%m/%Y"], issue="V2", scope="column", ambiguous=True),
            MapValues(id="a4", target="c", mapping={"ksa": "SA"}, issue="V1"),
            Deduplicate(id="a5", keys=["id"], groups=[[1, 2]], conflicting=True, issue="S8"),
        ]
        for action in actions:
            with self.subTest(action=action.type):
                self.assertEqual(action_from_dict(action.to_dict()), action)


if __name__ == "__main__":
    unittest.main()
