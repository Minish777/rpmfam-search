"""Тесты парсера rpmfam-search. Запуск: python -m unittest discover -s tests

Сеть не используется: всё проверяется на fixtures/sample.txt — синтетическом
документе с теми же граблями, что встречаются в живом (лишние скобки, буллиты,
склейка абзацев, «относится к», телефоны без пробела).
"""

import os
import tempfile
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rpmfam_search as r  # noqa: E402

SUGGESTED = r.SUGGESTED_DOC_ID

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fixtures", "sample.txt")

with open(FIXTURE, encoding="utf-8") as f:
    SAMPLE = f.read()

DB = r.parse(SAMPLE)
ES = r.entries(DB)
BY_NAME = {e["name"]: e for e in ES}


def people_of(name):
    e = BY_NAME[name]
    return [p for b in e["blocks"] for p in b["people"]]


class TestUtils(unittest.TestCase):
    def test_norm_collapses_spaces(self):
        self.assertEqual(r.norm("  Флоре      с  "), "Флоре с")

    def test_key_folds_case_and_yo(self):
        self.assertEqual(r.key("Ёлки"), "елки")

    def test_loose_strips_spaces(self):
        # баг из живого документа: фамилия набрана с пробелом внутри
        self.assertEqual(r.loose("Флоре с"), "flores")
        self.assertIn("flores", r.loose("Флоре с"))

    def test_loose_transliterates(self):
        self.assertEqual(r.loose("Амброус"), r.loose("Ambrous"))
        self.assertIn(r.loose("Амб"), r.loose("Амброус"))


class TestPersonName(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(r.parse_person_name("Алиса Тестова-Арч (LenaTest)"),
                         ("Алиса Тестова-Арч", "LenaTest"))

    def test_no_nick(self):
        self.assertEqual(r.parse_person_name("Лээре Макеев-Нейман"),
                         ("Лээре Макеев-Нейман", None))

    def test_unclosed_paren(self):
        """В документе есть '(DomovenokStopani0' без закрытой скобки."""
        self.assertEqual(r.parse_person_name("Руслан Уильямс (DomovenokStopani0"),
                         ("Руслан Уильямс", "DomovenokStopani0"))

    def test_extra_paren(self):
        """В документе есть '(Crackedon))' с лишней скобкой."""
        self.assertEqual(r.parse_person_name("Александр Порше (Crackedon))"),
                         ("Александр Порше", "Crackedon"))

    def test_inner_group_stripped(self):
        self.assertEqual(r.parse_person_name("Адам (nick) Фамилия (n2)"),
                         ("Адам Фамилия", "n2"))


class TestSurnames(unittest.TestCase):
    def test_all_surnames(self):
        self.assertEqual(len(ES), 29)

    def test_no_intro_text_as_surname(self):
        """Баг: вводный абзацы документа попадали в список как фамилии."""
        for e in ES:
            self.assertNotIn(" ", e["name"][:1])
            self.assertNotIn("Зарегистрированные", e["name"])
            self.assertFalse(e["name"].startswith("("))

    def test_every_note_bound(self):
        used = {m for s in DB["surnames"] for m in s["markers"]}
        self.assertEqual(set(DB["notes"]) - used, set())

    def test_two_markers_one_surname(self):
        e = BY_NAME["Бронзе"]
        self.assertEqual(e["markers"], ["d", "e"])
        self.assertTrue(e["star"])

    def test_glued_lines_split_correctly(self):
        """'Дрейхард\\n[bo]Дельта[g]' — сноска [bo] принадлежит Дрейхарду."""
        self.assertEqual(BY_NAME["Дрейхард"]["markers"], ["bo"])
        self.assertEqual(BY_NAME["Дельта"]["markers"], ["g"])
        self.assertEqual(people_of("Дрейхард")[0]["nick"], "IIITest")
        self.assertEqual(people_of("Дельта")[0]["nick"], "XAOCTest")

    def test_surname_without_note(self):
        self.assertIn("Астер", BY_NAME)
        self.assertEqual(BY_NAME["Астер"]["blocks"], [])

    def test_letter_headers_not_surnames(self):
        for letter in ("Б", "Г", "Д", "К", "Л", "С", "Ф", "Х", "Ц", "Щ"):
            if letter in BY_NAME:
                self.fail(f"буква {letter!r} попала в список фамилий")


class TestPeople(unittest.TestCase):
    def test_roles(self):
        heads = [p for p in people_of("Амброус") if p["role"] == r.ROLE_HEAD]
        deps = [p for p in people_of("Амброус") if p["role"] != r.ROLE_HEAD]
        self.assertEqual(len(heads), 1)
        self.assertEqual(len(deps), 1)

    def test_two_people_do_not_share_fields(self):
        """Регрессия: поля второго человека раньше затирали паспорт первого."""
        people = people_of("Калашников(а)")
        self.assertEqual(len(people), 2)
        self.assertEqual(people[0]["passport"], "RPM-000070")
        self.assertEqual(people[1]["passport"], "RPM-000071")

    def test_phone_without_space(self):
        """'Номер телефона -10000081' — без пробела после тире."""
        deputy = [p for p in people_of("Ланс") if p["nick"] == "_SalatTest"][0]
        self.assertEqual(deputy["phone"], "10000081")

    def test_bullet_and_indent_tolerated(self):
        """Google Docs превращает строку в пункт списка — разбор не должен ломаться."""
        deputy = [p for p in people_of("Холостов") if p["nick"] == "bulTest"][0]
        self.assertEqual(deputy["role"], "Заместитель")
        self.assertEqual(deputy["passport"], "RPM-000194")
        self.assertEqual(deputy["phone"], "10000194")

    def test_reactions_are_dropped(self):
        for p in people_of("Эскобар"):
            self.assertNotIn("🍀", p["name"])

    def test_unclosed_nick_extracted(self):
        self.assertEqual(people_of("Риверс")[0]["nick"], "craftTest")


class TestNotes(unittest.TestCase):
    def test_any_representative(self):
        e = BY_NAME["Бронзе"]
        self.assertTrue(e["star"])
        self.assertIn("любой представитель", e["note"])

    def test_star_from_note_without_asterisk(self):
        """Редактор забыл звёздочку в списке, но сноска её подтверждает."""
        e = BY_NAME["Эскеро"]
        self.assertTrue(e["star"])
        self.assertIn("при условии", e["note"])

    def test_caveat_detected(self):
        self.assertTrue(r.RE_CAVEAT.search(BY_NAME["Эскеро"]["note"]))

    def test_ref_forward(self):
        self.assertEqual(BY_NAME["Ходов"]["ref"], "Холлидей")
        self.assertIsNotNone(r.resolve_ref(ES, "Холлидей"))

    def test_ref_resolves_to_people(self):
        target = r.resolve_ref(ES, BY_NAME["Эскобар"]["ref"])
        self.assertTrue([p for b in target["blocks"] for p in b["people"]])

    def test_broken_ref_detected(self):
        """В документе бывает ссылка на фамилию, которой в списке уже нет."""
        e = BY_NAME["Якубов"]
        self.assertEqual(e["ref"], "Несуществующей")
        self.assertIsNone(r.resolve_ref(ES, e["ref"]))


class TestLinks(unittest.TestCase):
    def test_double_surname_detected(self):
        """Один и тот же человек значится в двух фамилиях (двойная фамилия)."""
        links = r.build_links(ES)
        rel = [r.key(x) for x in r.related(links, BY_NAME["Дельта"])]
        self.assertIn("дрейхард", rel)

    def test_no_self_reference(self):
        links = r.build_links(ES)
        for e in ES:
            self.assertNotIn(e["name"], r.related(links, e))


class TestSearch(unittest.TestCase):
    def test_exact_surname(self):
        kind, found, _, _ = r.do_search("Амброус", ES)
        self.assertEqual(kind, "surname")
        self.assertEqual(found[0]["name"], "Амброус")

    def test_case_insensitive(self):
        kind, found, _, _ = r.do_search("амброус", ES)
        self.assertEqual(kind, "surname")

    def test_partial(self):
        kind, contains, persons, numbers = r.do_search("Грин", ES)
        self.assertEqual(kind, "contains")
        self.assertIn("Гринч", [e["name"] for e in contains])

    def test_by_nick(self):
        _, contains, persons, _ = r.do_search("sqTest", ES)
        self.assertTrue(persons)
        self.assertEqual(persons[0][1]["nick"], "sqTest")

    def test_by_name(self):
        _, _, persons, _ = r.do_search("Хикару", ES)
        self.assertTrue(persons)

    def test_by_passport(self):
        _, contains, persons, numbers = r.do_search("000060", ES)
        self.assertTrue(numbers)
        self.assertEqual(numbers[0][1]["passport"], "RPM-000060")

    def test_by_phone(self):
        _, _, _, numbers = r.do_search("10000070", ES)
        self.assertTrue(numbers)

    def test_empty_query_matches_nothing(self):
        """Баг: пустой запрос вываливал всю базу — 598 строк."""
        for q in ("", " ", "\t"):
            kind, contains, persons, numbers = r.do_search(r.norm(q), ES)
            self.assertEqual(kind, "contains")
            self.assertEqual((contains, persons, numbers), ([], [], []))

    def test_suggestions_keep_original_case(self):
        """Баг: подсказки печатались в нижнем регистре."""
        near = r.suggestions("Бронзее", ES)
        self.assertIn("Бронзе", near)
        self.assertNotIn("бронзе", near)


class TestValidation(unittest.TestCase):
    def test_good_document_passes(self):
        self.assertEqual(r.registry_problems(DB), [])

    def test_html_rejected(self):
        """Баг: HTML-страница Google парсилась как фамилия и код был 0."""
        html = "<!DOCTYPE html><html><head><title>Sign in</title></head></html>"
        problems = r.registry_problems(r.parse(html))
        self.assertTrue(problems)
        self.assertIn("HTML", problems[0])

    def test_garbage_rejected(self):
        self.assertTrue(r.registry_problems(r.parse("просто текст")))

    def test_strict_rejects_small_document(self):
        """--doc не должен принимать документ, где нет ни фамилий, ни сносок."""
        small = r.parse("Здесь просто текст без реестра.")
        self.assertTrue(r.registry_problems(small, strict=True))


class TestDocId(unittest.TestCase):

    def test_full_url(self):
        self.assertEqual(
            r.extract_doc_id(
                "https://docs.google.com/document/d/1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA/edit?pli=1"),
            "1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA")

    def test_bare_id(self):
        self.assertEqual(r.extract_doc_id("1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA"),
                         "1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA")

    def test_quoted(self):
        self.assertEqual(
            r.extract_doc_id("<https://docs.google.com/document/d/1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA>"),
            "1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA")

    def test_garbage_rejected(self):
        for bad in ("привет", "abc123", "", "https://example.com/page"):
            self.assertIsNone(r.extract_doc_id(bad), bad)


class TestSnapshot(unittest.TestCase):
    def test_first_run_is_baseline(self):
        """Баг: на первом запуске все фамилии считались новыми."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.tsv")
            snap, fresh, gone, first = r.update_snapshot(path, ES)
            self.assertTrue(first)
            self.assertEqual(fresh, [])
            self.assertEqual(gone, [])
            self.assertEqual(len(snap), len(ES))

    def test_new_surname_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.tsv")
            r.update_snapshot(path, ES)
            added = ES + [{"name": "Новая", "markers": [], "star": False,
                           "blocks": [], "note": None, "ref": None}]
            snap, fresh, gone, first = r.update_snapshot(path, added)
            self.assertFalse(first)
            self.assertEqual(fresh, ["Новая"])
            self.assertEqual(gone, [])
            self.assertIn("Новая", snap)

    def test_removed_surname_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.tsv")
            r.update_snapshot(path, ES)
            fewer = ES[:-1]
            snap, fresh, gone, first = r.update_snapshot(path, fewer)
            self.assertEqual(fresh, [])
            self.assertEqual(gone, [ES[-1]["name"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFirstRun(unittest.TestCase):
    """Пока документ не выбран, утилита работать не должна."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("XDG_CONFIG_HOME", "XDG_CACHE_HOME")}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "conf")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_no_doc_means_no_context(self):
        self.assertIsNone(r.resolve_context()["doc_id"])

    def test_blocked_without_doc(self):
        """Любая команда без документа обязана падать с кодом 2."""
        quiet = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = quiet
        try:
            for args in ([], ["Амброус"], ["--all"], ["--check"], ["-r", "Амброус"]):
                self.assertEqual(r.main(args), 2, args)
        finally:
            sys.stdout = old
            quiet.close()

    def test_help_works_without_doc(self):
        quiet = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = quiet
        try:
            with self.assertRaises(SystemExit) as cm:
                r.main(["-h"])
        finally:
            sys.stdout = old
            quiet.close()
        self.assertEqual(cm.exception.code, 0)

    def test_doc_flag_works_without_doc(self):
        quiet = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = quiet
        try:
            self.assertEqual(r.main(["--doc"]), 0)
        finally:
            sys.stdout = old
            quiet.close()

    def test_connect_then_usable(self):
        r.connect_doc(SUGGESTED, "Тестовый")
        ctx = r.resolve_context()
        self.assertEqual(ctx["doc_id"], SUGGESTED)
        self.assertEqual(ctx["label"], "Тестовый")
        self.assertTrue(os.path.isfile(ctx["cache"]) or ctx["cache"].endswith(".txt"))

    def test_reset_clears_doc(self):
        r.connect_doc(SUGGESTED, "Тестовый")
        quiet = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = quiet
        try:
            r.main(["--doc", "reset"])
        finally:
            sys.stdout = old
            quiet.close()
        self.assertIsNone(r.resolve_context()["doc_id"])

    def test_clean_keeps_config_with_flag(self):
        r.connect_doc(SUGGESTED, "Тестовый")
        quiet = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = quiet
        try:
            r.main(["--clean", "--keep-config"])
        finally:
            sys.stdout = old
            quiet.close()
        self.assertIsNotNone(r.resolve_context()["doc_id"])


class TestDocumentQuirks(unittest.TestCase):
    """Огрехи самого документа: утилита их не чинит, а должна находить."""

    def test_suspicious_spaced(self):
        """«Флоре с» вместо «Флорес» — пробел посередине фамилии."""
        self.assertTrue(r.suspicious_spaced("Флоре с"))
        self.assertTrue(r.suspicious_spaced("Флоре  с"))

    def test_normal_names_not_flagged(self):
        for name in ("Сомов(а)", "Флорес", "Калашников(а)", "Акудзато",
                     "Мацумото", "Роуз-Уинстон", "Макеев-Нейман"):
            self.assertFalse(r.suspicious_spaced(name), name)

    def test_fixture_has_quirks(self):
        spaced = [e["name"] for e in ES if r.suspicious_spaced(e["name"])]
        self.assertIn("Флоре с", spaced)
        bad = [(e["name"], p["passport"]) for e in ES
               for b in e["blocks"] for p in b["people"]
               if p.get("passport") and not r.RE_PASSPORT_OK.match(p["passport"])]
        self.assertIn(("Холостов", "000193"), bad)

    def test_passport_format(self):
        self.assertTrue(r.RE_PASSPORT_OK.match("RPM-879252"))
        self.assertTrue(r.RE_PASSPORT_OK.match("rpm-879252"))
        # кириллические Х — не настоящий номер, а «пока неизвестно»
        self.assertIsNone(r.RE_PASSPORT_OK.match("RPM-ХХХХХХ"))
        self.assertFalse(r.RE_PASSPORT_OK.match("898999"))
        self.assertFalse(r.RE_PASSPORT_OK.match(""))
