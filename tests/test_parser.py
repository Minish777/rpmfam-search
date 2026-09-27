"""Тесты парсера rpmfam-search. Запуск: python -m unittest discover -s tests

Сеть не используется: всё проверяется на fixtures/sample.txt — синтетическом
документе с теми же граблями, что встречаются в живом (лишние скобки, буллиты,
склейка абзацев, «относится к», телефоны без пробела).
"""

import os
import tempfile
import time
import urllib.error
import urllib.request
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
        self.assertEqual(r.format_age(7200), "2 часов")
        self.assertEqual(r.format_age(259200), "3 дней")


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
