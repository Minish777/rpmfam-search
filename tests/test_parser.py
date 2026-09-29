"""Тесты парсера rpmfam-search. Запуск: python -m unittest discover -s tests

Сеть не используется и реальные файлы не трогаются: setUpModule уводит
XDG_CONFIG_HOME и XDG_CACHE_HOME во временный каталог, так что тесты
физически не могут испортить настройки и кеш пользователя. Всё проверяется
на fixtures/sample.txt — синтетическом документе с теми же граблями, что
встречаются в живом (лишние скобки, буллиты, склейка абзацев, «относится
к», телефоны без пробела).
"""

import contextlib
import io
import os
import tempfile
import time
import urllib.error
import urllib.request
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rpmfam_search as r  # noqa: E402

# Каталог на время прогона. Переопределяется в setUpModule, а пока —
# заглушка, чтобы импортируемый модуль ничего не создавал.
_TMP = tempfile.TemporaryDirectory()
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP.name, "conf")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP.name, "cache")

_REAL_XDG = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME")}


def setUpModule():
    """Весь прогон идёт во временном каталоге."""
    os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP.name, "conf")
    os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP.name, "cache")


def tearDownModule():
    for k, v in _REAL_XDG.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _TMP.cleanup()


def no_net():
    """Подмена сети: любой запрос падает, чтобы тест не зависел от неё."""
    def boom(*a, **kw):
        raise AssertionError("тест полез в сеть")
    return boom


SUGGESTED = r.SUGGESTED_DOC_ID
_REAL_FOREBEARS = r.forebears_coverage

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


class TestBanned(unittest.TestCase):
    """Запрет на выдачу: пока фамилия в документе — показываем, убрали — нет."""

    def test_known_ban_found(self):
        self.assertIn("ЗАБАНЕНО", r.ban_reason("Зетрикс"))

    def test_case_and_space_insensitive(self):
        for name in ("зетрикс", "ЗЕТРИКС", " Зетрикс "):
            self.assertIsNotNone(r.ban_reason(name), name)

    def test_other_surname_not_banned(self):
        for name in ("Амброус", "Лайт", ""):
            self.assertIsNone(r.ban_reason(name), name)

    def test_similar_name_not_banned(self):
        """Зитракс и Зетрикс — разные фамилии, различаются одной буквой.

        Запрет только на «Зетрикс», «Зитракс» выдавать можно. Сравнение
        строгое, намеренно: нечёткое здесь опасно.
        """
        self.assertIsNotNone(r.ban_reason("Зетрикс"))
        for name in ("Зитракс", "ЗИТРАКС", "Зитрекс", "Зетр"):
            self.assertIsNone(r.ban_reason(name), name)

    def test_absent_in_fixture_means_no_notice(self):
        """Пока фамилии нет в документе, пометка не должна нигде появляться."""
        hits = [e["name"] for e in ES if r.ban_reason(e["name"])]
        self.assertEqual(hits, [])

    def test_notice_appears_if_surname_present(self):
        with_ban = ES + [{"name": "Зетрикс", "markers": [], "star": False,
                          "blocks": [], "note": None, "ref": None}]
        hits = [e["name"] for e in with_ban if r.ban_reason(e["name"])]
        self.assertEqual(hits, ["Зетрикс"])

    def test_notice_mentions_the_other_spelling(self):
        """Фамилии в одну букву, поэтому запрет должен сам пояснять разницу."""
        reason = r.ban_reason("Зетрикс")
        self.assertIn("ЗИТРАКС", reason)
        self.assertIn("выдать можно", reason)

    def test_notice_lines_fit_terminal(self):
        """Пометка не должна уезжать за 80 колонок построчно."""
        for name in r.BANNED:
            for line in r.ban_reason(name).split("\n"):
                self.assertLessEqual(len(line), 80, line)


class TestUpdate(unittest.TestCase):
    """Проверка обновлений: без сети работает, лишних запросов не делает."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in
                       ("XDG_CONFIG_HOME", "XDG_CACHE_HOME")}
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "conf")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")
        self._real = r.fetch_latest_version
        self.calls = []

    def tearDown(self):
        r.fetch_latest_version = self._real
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _fake(self, version):
        def fetch(timeout=8.0):
            self.calls.append(1)
            return version
        r.fetch_latest_version = fetch

    def test_version_present(self):
        self.assertRegex(r.__version__, r"^\d+\.\d+")

    def test_no_network_no_crash(self):
        self._fake(None)
        self.assertIsNone(r.check_update(force=True))

    def test_cached_between_runs(self):
        """Сеть дёргается не чаще раза в UPDATE_INTERVAL."""
        self._fake("9.9.9")
        r.check_update(force=True)
        self.assertEqual(len(self.calls), 1)
        for _ in range(5):
            r.check_update()
        self.assertEqual(len(self.calls), 1)

    def test_pending_detection(self):
        self.assertTrue(r.update_pending("9.9.9"))
        self.assertFalse(r.update_pending(r.__version__))
        self.assertFalse(r.update_pending(None))

    def test_keeps_last_known_on_failure(self):
        self._fake("9.9.9")
        r.check_update(force=True)
        self._fake(None)
        self.assertEqual(r.check_update(force=True), "9.9.9")

    def test_detect_install(self):
        self.assertIn(r.detect_install(), ("git", "pip", "unknown"))

    def test_notice_suppressed_for_service_flags(self):
        for flag in ("-h", "--help", "--clean", "-u", "--update", "--version"):
            self.assertIn(flag, r.QUIET_NOTICE, flag)


class TestUpdateDetection(unittest.TestCase):
    """Регрессии из реальной установки, а не из теории."""

    def test_symlinked_launcher_resolves(self):
        """Через symlink в ~/.local/bin обновление должно работать.

        Реальный баг: module_dir() брал abspath и оставался в каталоге
        symlink, где нет .git, поэтому -u отвечал «unknown-установка».
        """
        self.assertTrue(r.git_root(), "каталог репозитория не найден")
        self.assertEqual(r.detect_install(), "git")

    def test_git_root_contains_module(self):
        root = r.git_root()
        self.assertTrue(os.path.isfile(os.path.join(root, "rpmfam_search.py")))

    def test_version_flags(self):
        for flag in ("-v", "-V", "--version"):
            quiet = open(os.devnull, "w")
            old = sys.stdout
            sys.stdout = quiet
            try:
                with self.assertRaises(SystemExit) as cm:
                    r.main([flag])
            finally:
                sys.stdout = old
                quiet.close()
            self.assertEqual(cm.exception.code, 0, flag)

    def test_version_flags_quiet(self):
        for flag in ("-v", "-V"):
            self.assertIn(flag, r.QUIET_NOTICE, flag)


class TestVersionCompare(unittest.TestCase):
    """Регрессия: утилита предлагала обновиться НА СТАРУЮ версию."""

    def test_parse_version(self):
        self.assertEqual(r.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(r.parse_version("1.10.0"), (1, 10, 0))
        self.assertEqual(r.parse_version(""), (0,))
        self.assertEqual(r.parse_version(None), (0,))

    def test_newer_remote_pending(self):
        # строим версию заведомо новее текущей, а не хардкодим число
        major, minor, patch = r.parse_version(r.__version__)
        newer = f"{major}.{minor}.{patch + 1}"
        self.assertTrue(r.update_pending(newer))
        self.assertTrue(r.update_pending("99.0.0"))

    def test_same_version_not_pending(self):
        self.assertFalse(r.update_pending(r.__version__))

    def test_older_remote_not_pending(self):
        """Откат на сервере или устаревший кэш не должны просить обновиться."""
        major, minor, patch = r.parse_version(r.__version__)
        for older in ("0.0.1", f"{major}.{minor}.{max(patch - 1, 0)}"):
            self.assertFalse(r.update_pending(older), older)

    def test_no_version_not_pending(self):
        self.assertFalse(r.update_pending(None))
        self.assertFalse(r.update_pending(""))


class TestFetchResilience(unittest.TestCase):
    """Сеть моргает и лежит — утилита обязана выдавать результат."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")
        self._real = urllib.request.urlopen
        self.attempts = []
        self._sleep = time.sleep
        time.sleep = lambda *_: None      # не ждать в тестах

    def tearDown(self):
        urllib.request.urlopen = self._real
        time.sleep = self._sleep
        if self._saved is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved
        self.tmp.cleanup()

    def _boom(self, *a, **kw):
        self.attempts.append(1)
        raise urllib.error.URLError("соединение оборвалось")

    def _ok(self, *a, **kw):
        self.attempts.append(1)
        payload = SAMPLE.encode("utf-8")

        class R:
            def read(self, *_):
                return payload
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        return R()

    def test_retries_then_succeeds(self):
        """Моргнуло и ожило: повтор спасает, кеш не понадобился."""
        urllib.request.urlopen = self._boom
        doc = os.path.join(self.tmp.name, "d.txt")
        r._write_private(doc, SAMPLE)
        state = {"n": 0}

        def flaky(*a, **kw):
            state["n"] += 1
            if state["n"] < 3:
                return self._boom()
            return self._ok()

        urllib.request.urlopen = flaky
        self.assertIn("Амброус", r.fetch("id", doc, True))
        self.assertEqual(len(self.attempts), 3)

    def test_all_attempts_fail_falls_back_to_cache(self):
        urllib.request.urlopen = self._boom
        doc = os.path.join(self.tmp.name, "d.txt")
        r._write_private(doc, SAMPLE)
        self.assertIn("Амброус", r.fetch("id", doc, True))
        self.assertEqual(len(self.attempts), r.FETCH_ATTEMPTS)

    def test_breaker_opens_and_shortens_next_try(self):
        """После неудач второй запуск не должен ждать все попытки."""
        urllib.request.urlopen = self._boom
        doc = os.path.join(self.tmp.name, "d.txt")
        r._write_private(doc, SAMPLE)
        r.fetch("id", doc, True)
        self.assertTrue(r.net_is_down())
        self.attempts.clear()
        r.fetch("id", doc, True)
        self.assertEqual(len(self.attempts), 1, "предохранитель не сработал")

    def test_success_clears_breaker(self):
        doc = os.path.join(self.tmp.name, "d.txt")
        r._write_private(doc, SAMPLE)
        r.mark_net_down()
        self.assertTrue(r.net_is_down())
        urllib.request.urlopen = self._ok
        r.fetch("id", doc, True)
        self.assertFalse(r.net_is_down(), "сеть ожила, предохранитель не сброшен")

    def test_format_age(self):
        self.assertEqual(r.format_age(30), "30 секунд")
        self.assertEqual(r.format_age(300), "5 минут")
        self.assertEqual(r.format_age(7200), "2 часа")
        self.assertEqual(r.format_age(259200), "3 дня")


class TestWordStartMatching(unittest.TestCase):
    """Регрессия: «Хейс» находил «Джейс» и выдавал чужую фамилию.

    Причина — поиск подстроки после транслитерации: heys сидит внутри
    dzheys. Совпадение засчитывается только с начала слова.
    """

    def test_not_inside_a_word(self):
        self.assertFalse(r.text_matches("Хейс", "Джейс Харт"))
        self.assertFalse(r.text_matches("хар", "Аки Акулин"))

    def test_word_start_matches(self):
        self.assertTrue(r.text_matches("Хейз", "Люк Хейз-Блэйд"))
        self.assertTrue(r.text_matches("Харт", "Джейс Харт"))
        self.assertTrue(r.text_matches("Хейс", "Джейс Хейс"))
        self.assertTrue(r.text_matches("Амб", "Винс Амброус"))

    def test_hyphen_is_a_boundary(self):
        """Дефис — не буква, поэтому части двойной фамилии ищутся отдельно."""
        self.assertTrue(r.text_matches("Арч", "Алиса Блэйд-Арч"))
        self.assertTrue(r.text_matches("Хёдо", "Крисоль Вендеркольт-Хёдо"))
        self.assertTrue(r.text_matches("Блэйд-Арч", "Алиса Блэйд-Арч"))

    def test_full_phrase_spanning_words(self):
        self.assertTrue(r.text_matches("Винс Амбр", "Винс Амброус"))
        self.assertTrue(r.text_matches("Алиса Блэйд", "Алиса Блэйд-Арч"))

    def test_query_inside_own_word(self):
        self.assertFalse(r.text_matches("ейс", "Джейс"))
        self.assertTrue(r.text_matches("Джейс", "Джейс Харт"))

    def test_transliterated_alphabet(self):
        self.assertTrue(r.text_matches("Ambrous", "Винс Амброус"))
        self.assertFalse(r.text_matches("Mbrus", "Винс Амброус"))
        self.assertFalse(r.text_matches("Ambros", "Винс Амброус"))  # неверная транслитерация

    def test_no_empty_query_match(self):
        self.assertFalse(r.text_matches("", "Джейс Харт"))
        self.assertFalse(r.matches_at_word_start("", "что-то"))

    def test_every_surname_finds_itself(self):
        """Главная гарантия: точное имя всегда находится само по себе."""
        for e in ES:
            kind, found, _, _ = r.do_search(e["name"], ES)
            if kind == "surname":
                self.assertEqual(found[0]["name"], e["name"])
            else:
                self.assertIn(e["name"], {f["name"] for f in found})

    def test_known_false_positive_gone(self):
        """Конкретный случай из жалобы: «Хейс» не должен давать «Харт»."""
        kind, found, persons, _ = r.do_search("Хейс", ES)
        fams = {e["name"] for e in found} | {e["name"] for e, _ in persons}
        self.assertNotIn("Харт", fams)

    def test_loose_words_keeps_boundaries(self):
        """loose() склеивает слова, loose_words() — нет. Разница важна."""
        self.assertEqual(r.loose("Винс Амброус"), "vinsambrous")
        self.assertEqual(r.loose_words("Винс Амброус"), "vins ambrous")
        self.assertEqual(r.loose_words("Блэйд-Арч"), "bleyd arch")

    def test_cyrillic_does_not_hit_latin_nick(self):
        """«Аливе» -> alive -> ник «_aLIVEshka_». Мост только в одну сторону."""
        self.assertFalse(r.text_matches("Аливе",
                                        "Волет Гудман-Воронов _aLIVEshka_"))

    def test_latin_still_finds_cyrillic(self):
        """Обратное направление ломать нельзя: так ищут латиницей."""
        self.assertTrue(r.text_matches("Ambrous", "Винс Амброус"))
        self.assertTrue(r.text_matches("Voronov", "Егор Воронов"))

    def test_script_compatible(self):
        self.assertFalse(r.script_compatible("Аливе", "aliveshka"))
        self.assertTrue(r.script_compatible("Аливе", "аливе"))
        self.assertTrue(r.script_compatible("alive", "aliveshka"))
        self.assertTrue(r.script_compatible("Ambrous", "амброус"))


class TestSuggestions(unittest.TestCase):
    """Подсказки при опечатке: без мусора, верная — первой.

    Свои фамилии, а не из фикстуры: тест не должен зависеть от того,
    какие фамилии лежат в синтетическом документе.
    """

    NAMES = ["Хейз", "Грейс", "Дейвис", "Дейви", "Амброус", "Браун",
             "Брайн", "Алиев", "Алчев", "Столь", "Лайт", "Лайв"]

    def setUp(self):
        self.es = [{"name": n, "star": False, "blocks": [], "note": None,
                    "ref": None, "markers": []} for n in self.NAMES]

    def test_typo_gives_exact_answer(self):
        self.assertEqual(r.suggestions("Хейс", self.es), ["Хейз"])
        self.assertEqual(r.suggestions("Аливе", self.es), ["Алиев"])
        self.assertEqual(r.suggestions("Столь", self.es), ["Столь"])

    def test_no_junk_alongside(self):
        """Раньше к «Хейс» прилетали Грейс и Дейвис."""
        got = r.suggestions("Хейс", self.es)
        self.assertNotIn("Грейс", got)
        self.assertNotIn("Дейвис", got)

    def test_longer_typo_still_suggested(self):
        """«Амбру» -> «Амброус»: длина отличается на два, послабление нужно."""
        self.assertIn("Амброус", r.suggestions("Амбру", self.es))

    def test_similar_names_both_shown(self):
        """Омонимы не отличить — показываем обе, это честно."""
        got = r.suggestions("Брану", self.es)
        self.assertIn("Браун", got)
        self.assertIn("Брайн", got)

    def test_nonsense_gets_nothing(self):
        for q in ("Амбр", "Фывапр", "ывап", "щщщ"):
            self.assertEqual(r.suggestions(q, self.es), [], q)

    def test_keeps_original_case(self):
        for s in r.suggestions("Хейс", self.es):
            self.assertEqual(s, s.strip())
            self.assertFalse(s.islower())

    def test_limit_respected(self):
        for q in ("Лай", "Дейви", "Стол"):
            self.assertLessEqual(len(r.suggestions(q, self.es)), r.SUGGEST_LIMIT)

    def test_score_rejects_unrelated(self):
        self.assertIsNone(r._suggest_score("Хейс", "Зоркий"))
        self.assertIsNone(r._suggest_score("", "Хейз"))

    def test_prefix_wins_over_raw_ratio(self):
        """«Хейс» и «Грейс» похожи по буквам, но префикс решает."""
        good = r._suggest_score("Хейс", "Хейз")
        self.assertIsNotNone(good)
        self.assertIsNone(r._suggest_score("Хейс", "Грейс"))


class TestFlagTypo(unittest.TestCase):
    """Клавиатура в русской раскладке: --сheck вместо --check."""

    def test_cyrillic_homoglyphs_fixed(self):
        self.assertEqual(r.fix_flags(["--сheck"]), ["--check"])
        self.assertEqual(r.fix_flags(["--аll"]), ["--all"])
        self.assertEqual(r.fix_flags(["-А"]), ["-A"])

    def test_query_untouched(self):
        """«Арч» — это Арч, а не Ap4. Флаги чистим, запрос не трогаем."""
        self.assertEqual(r.fix_flags(["Арч", "Хейс", "Уинстон"]),
                         ["Арч", "Хейс", "Уинстон"])

    def test_values_of_flags_untouched(self):
        """Ссылка с кириллицей не должна поехать."""
        self.assertEqual(r.fix_flags(["--name", "Фамы Юга"]),
                         ["--name", "Фамы Юга"])

    def test_single_dash_kept(self):
        self.assertEqual(r.fix_flags(["-"]), ["-"])

    def test_cyrillic_check_flag_actually_runs(self):
        """Именно тот случай из жалобы: --сheck должен работать.

        Сеть подменена: тест проверяет разбор флага, а не доступ к Google.
        """
        real = r.fetch
        r.fetch = lambda *a, **kw: SAMPLE
        r.connect_doc(SUGGESTED, "тест")
        quiet = open(os.devnull, "w")
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = quiet
        try:
            code = r.main(["--сheck"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            quiet.close()
            r.fetch = real
        self.assertEqual(code, 0)

    def test_unknown_flag_lists_available(self):
        """Вместо голого «unrecognized» перечисляем флаги."""
        quiet = open(os.devnull, "w")
        old = sys.stderr
        sys.stderr = quiet
        try:
            with self.assertRaises(SystemExit):
                r.run(["--совсем-не-такой-флаг"])
        finally:
            sys.stderr = old
            quiet.close()


class TestSuiteIsolation(unittest.TestCase):
    """Тесты не должны трогать настоящие настройки пользователя.

    README предлагает запускать тесты, значит любой из них обязан быть
    безопасен: сеть не трогаем, файлы — только во временном каталоге.
    """

    def test_xdg_pointed_into_temp(self):
        for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            self.assertIn(_TMP.name, os.environ.get(k, ""), k)

    def test_no_net_helper_exists(self):
        """Подмена сети используется тестами загрузки — проверяем, что есть."""
        self.assertTrue(callable(no_net()))


class TestPlurals(unittest.TestCase):
    """Русские числительные: было «1 часов», «2 дней»."""

    def test_plural_forms(self):
        self.assertEqual(r.plural(1, "час", "часа", "часов"), "1 час")
        self.assertEqual(r.plural(2, "час", "часа", "часов"), "2 часа")
        self.assertEqual(r.plural(5, "час", "часа", "часов"), "5 часов")
        self.assertEqual(r.plural(11, "час", "часа", "часов"), "11 часов")
        self.assertEqual(r.plural(21, "час", "часа", "часов"), "21 час")
        self.assertEqual(r.plural(22, "час", "часа", "часов"), "22 часа")
        self.assertEqual(r.plural(25, "час", "часа", "часов"), "25 часов")
        self.assertEqual(r.plural(101, "час", "часа", "часов"), "101 час")
        self.assertEqual(r.plural(111, "час", "часа", "часов"), "111 часов")

    def test_format_age_reads_naturally(self):
        cases = {0: "1 секунда", 1: "1 секунда", 5: "5 секунд",
                 90: "1 минута", 300: "5 минут", 5400: "1 час",
                 7200: "2 часа", 172800: "2 дня", 259200: "3 дня"}
        for secs, want in cases.items():
            self.assertEqual(r.format_age(secs), want, secs)


class TestPassportFormats(unittest.TestCase):
    """Номер ищется в том виде, в каком он записан в документе."""

    def setUp(self):
        self.es = [{"name": "Тест", "star": False, "markers": [], "ref": None,
                    "note": None, "blocks": [{"people": [
                        {"role": "Глава", "name": "Кто-то", "nick": "nick",
                         "passport": "RPM-910442", "phone": "14882930"}], "note": None}]}]

    def test_variants_accepted(self):
        for q in ("910442", "RPM-910442", "rpm-910442", "RPM - 910442"):
            _, _, _, numbers = r.do_search(q, self.es)
            self.assertTrue(numbers, q)
            self.assertEqual(numbers[0][1]["passport"], "RPM-910442", q)

    def test_partial_number(self):
        _, _, _, numbers = r.do_search("9104", self.es)
        self.assertTrue(numbers)

    def test_phone_still_found(self):
        _, _, _, numbers = r.do_search("14882930", self.es)
        self.assertTrue(numbers)

    def test_non_number_not_treated_as_number(self):
        _, _, _, numbers = r.do_search("Амб", self.es)
        self.assertEqual(numbers, [])


class TestCrossPlatform(unittest.TestCase):
    """Пути под каждую платформу и устойчивость к правам на каталоги."""

    def probe(self, plat, env):
        import ntpath, posixpath
        old = sys.platform, os.path, os.path.expanduser, dict(os.environ)
        sys.platform = plat
        os.path = ntpath if plat == "win32" else posixpath
        if plat == "win32":
            os.path.expanduser = lambda p: p.replace("~", r"C:\Users\t")
        for k in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            return r.config_dir(), r.cache_dir()
        finally:
            sys.platform, os.path, os.path.expanduser, _ = (
                old[0], old[1], old[2], old[3])
            os.environ.clear()
            os.environ.update(old[3])

    def test_windows_uses_appdata(self):
        c, k = self.probe("win32", {
            "APPDATA": r"C:\Users\t\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\t\AppData\Local"})
        self.assertIn("Roaming", c)
        self.assertIn("Local", k)
        self.assertIn("rpmfam-search", c)
        self.assertIn("rpmfam-search", k)

    def test_windows_without_env_falls_back(self):
        c, k = self.probe("win32", {})
        self.assertTrue(c.startswith("C:"))
        self.assertTrue(k.startswith("C:"))

    def test_macos_uses_library_caches(self):
        _, k = self.probe("darwin", {})
        self.assertIn("Library", k)

    def test_linux_xdg(self):
        c, k = self.probe("linux", {"XDG_CONFIG_HOME": "/cfg",
                                    "XDG_CACHE_HOME": "/cache"})
        self.assertEqual(c, "/cfg/rpmfam-search")
        self.assertEqual(k, "/cache/rpmfam-search")

    def test_linux_without_xdg(self):
        c, k = self.probe("linux", {})
        self.assertIn("/.config/", c)
        self.assertIn("/.cache/", k)

    def test_cache_never_in_config(self):
        """Кеш и настройки — разные каталоги, иначе --clean снесёт оба."""
        for plat, env in (("win32", {"APPDATA": r"C:\A", "LOCALAPPDATA": r"C:\B"}),
                          ("darwin", {}),
                          ("linux", {})):
            c, k = self.probe(plat, env)
            self.assertNotEqual(c, k, plat)

    def test_fetch_works_when_cache_unwritable(self):
        """Каталог может быть только для чтения — результат всё равно нужен."""
        real = r._write_private
        r._write_private = lambda *a, **kw: (_ for _ in ()).throw(
            OSError(13, "Permission denied"))
        class Resp:
            def read(self, *_):
                return SAMPLE.encode()
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        real_open = r.urllib.request.urlopen
        r.urllib.request.urlopen = lambda *a, **kw: Resp()
        r.mark_net_down()
        quiet = open(os.devnull, "w")
        old = sys.stderr
        sys.stderr = quiet
        try:
            got = r.fetch("id", os.path.join(_TMP.name, "нет", "doc.txt"), True)
        finally:
            sys.stderr = old
            quiet.close()
            r._write_private = real
            r.urllib.request.urlopen = real_open
        self.assertIn("Амброус", got)

    def test_connect_doc_reports_write_failure(self):
        real = r._write_private
        r._write_private = lambda *a, **kw: (_ for _ in ()).throw(
            OSError(13, "Permission denied"))
        try:
            err = r.connect_doc("id", "метка")
        finally:
            r._write_private = real
        self.assertIsNotNone(err)
        self.assertIn("прав", err)


class TestMatchHighlight(unittest.TestCase):
    """Показываем, где именно совпало: «Данила Йегер-[Стоун]»."""

    def setUp(self):
        self.person = {"role": "Глава", "name": "Данила Йегер-Стоун",
                       "nick": "scorpion228337", "passport": "RPM-585807",
                       "phone": "22833799"}
        self.nick_person = {"role": "Глава", "name": "Винс Амброус",
                            "nick": "sqW1nz", "passport": None, "phone": None}

    def test_marks_matched_word(self):
        self.assertEqual(r.mark_match("Стоун", self.person["name"]),
                         "Данила Йегер-[Стоун]")

    def test_marks_with_capital_letter(self):
        """Раньше заглавная «Х» не находилась в нижнем регистре."""
        self.assertEqual(r.mark_match("Хёдо", "Крисоль Вендеркольт-Хёдо"),
                         "Крисоль Вендеркольт-[Хёдо]")

    def test_marks_nick(self):
        self.assertEqual(r.mark_match("sqW1nz", self.nick_person["nick"]),
                         "[sqW1nz]")

    def test_no_match_returns_plain(self):
        self.assertEqual(r.mark_match("Зоркий", self.person["name"]),
                         self.person["name"])

    def test_empty_text(self):
        self.assertIsNone(r.mark_match("Стоун", None))

    def test_match_kind(self):
        self.assertEqual(r.match_kind("Стоун", self.person), "фамилии")
        self.assertEqual(r.match_kind("Йегер", self.person), "фамилии")
        self.assertEqual(r.match_kind("sqW1nz", self.nick_person), "нике")
        self.assertEqual(r.match_kind("Винс", self.nick_person), "имени")

    def test_marked_person_keeps_all_data(self):
        out = r.fmt_person_marked(self.person, "Стоун")
        self.assertIn("[Стоун]", out)
        self.assertIn("scorpion228337", out)
        self.assertIn("RPM-585807", out)
        self.assertIn("22833799", out)


class TestPersonHitOutput(unittest.TestCase):
    """Вывод находок по людям не должен выдавать семью за ответ."""

    def render(self, query, persons, contains=(), numbers=()):
        import io, contextlib
        r.C.on = False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r.print_person_hits(query, persons, list(contains), list(numbers), False)
        r.C.on = True
        return buf.getvalue()

    def setUp(self):
        self.entry = {"name": "Йегер", "star": False, "markers": [],
                      "blocks": [], "note": None, "ref": None}
        self.person = (self.entry,
                       {"role": "Глава", "name": "Данила Йегер-Стоун",
                        "nick": "scorpion228337",
                        "passport": "RPM-585807", "phone": "22833799"})

    def test_surname_not_registered_is_the_answer(self):
        """Главное — что выдать нельзя, а не какая семья нашлась."""
        out = self.render("Стоун", [self.person])
        self.assertIn("не зарегистрирована", out)
        self.assertIn("нельзя", out)

    def test_no_answer_shaped_label(self):
        """Метка «Зарегестрированная фамилия» тут вводит в заблуждение."""
        out = self.render("Стоун", [self.person])
        self.assertNotIn("Зарегестрированная фамилия", out)

    def test_matched_word_marked(self):
        out = self.render("Стоун", [self.person])
        self.assertIn("[Стоун]", out)
        self.assertIn("Йегер", out)

    def test_nick_gets_different_wording(self):
        out = self.render("scorpion228337", [self.person])
        self.assertNotIn("не зарегистрирована", out)
        self.assertIn("найден", out)

    def test_surname_vs_nick_detection(self):
        self.assertTrue(r.looks_like_surname("Стоун"))
        self.assertFalse(r.looks_like_surname("scorpion228337"))
        self.assertFalse(r.looks_like_surname("910442"))

    def test_mixed_query_counted_as_surname(self):
        """Запрос с кириллицей — это про фамилию."""
        self.assertTrue(r.looks_like_surname("Кингсманн"))


def report(name, entries=ES, use_net=False):
    """Прогоняет проверку имени и возвращает текст отчёта."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r.print_name_report(name, entries, {}, use_net)
    return buf.getvalue()


class TestNameCheckReport(unittest.TestCase):
    """Отчёт --check-name собирает правила, реестр и ручную проверку."""

    def test_registered_surname_needs_approval(self):
        out = report("Винс Амброус")
        self.assertIn("есть в реестре", out)
        self.assertIn("Главы или Зама", out)
        self.assertNotIn("ПАСПОРТ ВЫДАВАТЬ НЕЛЬЗЯ", out)

    def test_double_surname_prefix_is_not_registration(self):
        """«Тестов» — только начало «Тестова-Арч», регистрации нет.

        Раньше такое ложное срабатывание требовало одобрения Главы
        на пустом месте.
        """
        out = report("Пётр Тестов")
        self.assertIn("фамилии в реестре нет", out)
        self.assertNotIn("есть в реестре", out)
        self.assertNotIn("Главы или Зама", out)

    def test_unknown_surname(self):
        out = report("Алекс Вендетто")
        self.assertIn("фамилии в реестре нет", out)
        self.assertIn("Discord", out)

    def test_ban_blocks_before_anything_else(self):
        """Запрет в реестре обязан быть в самом верху, а не в хвосте.

        Проверяем на синтетической записи, чтобы тест не зависел от того,
        есть ли «Зетрикс» в живом документе.
        """
        entries = [dict(ES[0], name="Зетрикс")]
        out = report("Пётр Зетрикс", entries)
        self.assertIn("ПАСПОРТ ВЫДАВАТЬ НЕЛЬЗЯ", out)
        self.assertNotIn("Грубых нарушений не найдено", out)
        head = out[:out.index("ПАСПОРТ")]
        self.assertNotIn("есть в реестре", head)

    def test_local_rule_blocks(self):
        out = report("Иван Сукачёв")
        self.assertIn("ПАСПОРТ ВЫДАВАТЬ НЕЛЬЗЯ", out)
        self.assertIn("Мат", out)

    def test_clean_name_is_not_a_clean_result(self):
        """Нет блоков — это не «можно выдавать», а «нарушений не нашли»."""
        out = report("Алекс Вендетто")
        self.assertIn("не найдено", out)
        self.assertIn("Это не разрешение", out)

    def test_manual_checklist_always_present(self):
        for name in ("Алекс Вендетто", "Иван Сукачёв"):
            out = report(name)
            self.assertIn("Что проверить вручную", out)
            self.assertIn("2000", out)


class TestNameCheckNetwork(unittest.TestCase):
    """Без --forebears сеть не трогается: forebears рейтлимитит."""

    def test_no_forebears_request_by_default(self):
        r.forebears_coverage = no_net()
        report("Алекс Вендетто", use_net=False)

    def test_forebears_called_when_asked(self):
        calls = []

        def fake(kind, word):
            calls.append((kind, word))
            return 5000
        r.forebears_coverage = fake
        try:
            report("Алекс Вендетто", use_net=True)
        finally:
            r.forebears_coverage = _REAL_FOREBEARS
        self.assertTrue(calls)
        self.assertIn(("surnames", "Вендетто"), calls)
        self.assertIn(("name", "Алекс"), calls)

    def test_low_coverage_blocks(self):
        r.forebears_coverage = lambda k, w: 10
        try:
            out = report("Алекс Вендетто", use_net=True)
        finally:
            r.forebears_coverage = _REAL_FOREBEARS
        self.assertIn("меньше 2000", out)

    def test_unknown_coverage_not_treated_as_low(self):
        """Пустой ответ сайта — это «не смогли», а не «носителей мало»."""
        r.forebears_coverage = lambda k, w: None
        try:
            out = report("Алекс Вендетто", use_net=True)
        finally:
            r.forebears_coverage = _REAL_FOREBEARS
        self.assertNotIn("меньше 2000", out)
        self.assertIn("не удалось", out)


class TestForebearsUrl(unittest.TestCase):
    """Путь у фамилий /surnames/, у имён /name/ — не /names/."""

    def test_surnames_path(self):
        u = r.forebears_url("surnames", "Вендетто")
        self.assertIn("/surnames/vendetto", u)

    def test_name_path_singular(self):
        u = r.forebears_url("name", "Алекс")
        self.assertIn("/name/aleks", u)
        self.assertNotIn("/names/", u)


class TestCleanRemovesForebearsCache(unittest.TestCase):
    def test_forebears_tsv_deleted(self):
        cache = r.cache_dir()
        os.makedirs(cache, exist_ok=True)
        p = os.path.join(cache, "forebears.tsv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("surnames:vendetto\t93\n")
        r.clean(keep_config=True)
        self.assertFalse(os.path.exists(p))
