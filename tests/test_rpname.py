"""Тесты проверки РП имени по правилам мерии.

Примеры взяты прямо из правил и из жалоб сотрудников: цвета, профессии,
языки, страны, рофлофамилии, сокращённые имена и завуалированные слова
вроде «Цун Дере» или «Адо Альгит-Лер».

    python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rpname  # noqa: E402


def blocked(name):
    return [f for f in rpname.check_name(name) if f.level == "block"]


def titles(name):
    return {f.title for f in blocked(name)}


class TestClean(unittest.TestCase):
    def test_strips_nick_in_brackets(self):
        self.assertEqual(rpname.clean_name("Алекс Вендетто [LSTA]"), "Алекс Вендетто")
        self.assertEqual(rpname.clean_name("Алекс Вендетто (LSTA)"), "Алекс Вендетто")

    def test_strips_tag(self):
        self.assertEqual(rpname.clean_name("#Алекс Вендетто"), "Алекс Вендетто")

    def test_collapses_spaces(self):
        self.assertEqual(rpname.clean_name("  Алекс   Вендетто "), "Алекс Вендетто")


class TestGoodNames(unittest.TestCase):
    """То, что пропускать надо, не должно ловиться."""

    def test_normal_name(self):
        self.assertEqual(blocked("Алекс Вендетто"), [])

    def test_double_surname(self):
        self.assertEqual(blocked("Алекс Петров-Водкин"), [])

    def test_real_surnames_ending_in_skiy(self):
        """-ский бывает у настоящих фамилий, рубить все подряд нельзя."""
        for n in ("Пётр Мазурский", "Алекс Высоцкий", "Иван Пушкинский"):
            self.assertEqual(blocked(n), [], n)

    def test_real_surnames_similar_to_banned(self):
        for n in ("Пётр Стомачёв", "Алекс Романов", "Пётр Тимофеев"):
            self.assertEqual(blocked(n), [], n)

    def test_surname_with_yo(self):
        self.assertEqual(blocked("Пётр Стомачёв"), [])


class TestFormat(unittest.TestCase):
    def test_underscore(self):
        self.assertIn("Нижнее подчёркивание", titles("Алекс_Вендетто"))

    def test_lowercase(self):
        self.assertIn("Регистр букв", titles("алекс вендетто"))

    def test_latin_blocked(self):
        self.assertIn("Лишние символы в имени", titles("Alex Vendetto"))

    def test_digits_blocked(self):
        self.assertIn("Лишние символы в имени", titles("Алекс Вендетто2"))

    def test_triple_surname(self):
        self.assertTrue(titles("Алекс Петров-Водкин-Лукин"), "тройная фамилия")

    def test_single_word_never_says_missing_surname(self):
        """Одно слово трактуется как фамилия, а не как забытое имя."""
        self.assertNotIn("Нет фамилии", titles("Алекс"))

    def test_empty(self):
        self.assertIn("Пустое имя", titles("   "))

    def test_hyphen_parts_capitalized(self):
        """Дефис не должен ломать проверку регистра."""
        self.assertEqual(blocked("Алекс Петров-Водкин"), [])
        self.assertIn("Регистр букв", titles("Алекс петров-Водкин"))


class TestShortNames(unittest.TestCase):
    def test_short_first_names_blocked(self):
        for n in ("Лиза Петрова", "Тёма Соколов", "Миша Иванов", "Вовчик Петров",
                  "Серега Кузнецов", "Настя Морозова", "Дима Орлов"):
            self.assertIn("Имя не в полной форме", titles(n), n)

    def test_suggests_full_form(self):
        f = [x for x in blocked("Лиза Петрова") if x.title == "Имя не в полной форме"][0]
        self.assertIn("Елизавета", f.detail)

    def test_fix_field_suggestion(self):
        f = [x for x in blocked("Лиза Петрова") if x.title == "Имя не в полной форме"][0]
        self.assertTrue(f.fix)

    def test_full_names_pass(self):
        for n in ("Елизавета Петрова", "Михаил Иванов", "Владимир Петров"):
            self.assertEqual(blocked(n), [], n)


class TestBannedSurnames(unittest.TestCase):
    def test_colors(self):
        for n in ("Иван Белый", "Никита Черный", "Пётр Синий", "Алекс Зелёный"):
            self.assertIn("Цвет вместо фамилии", titles(n), n)

    def test_professions(self):
        for n in ("Антон Рыбаков", "Денис Инженеров", "Пётр Строитель",
                  "Алекс Слесарев"):
            self.assertIn("Профессия или должность вместо фамилии", titles(n), n)

    def test_languages(self):
        for n in ("Пётр Китайский", "Алекс Исламский", "Иван Английский"):
            self.assertIn("Язык вместо фамилии", titles(n), n)

    def test_places(self):
        for n in ("Иван Московский", "Алекс Питерский", "Пётр Сибирский"):
            self.assertIn("Страна, город или регион вместо фамилии", titles(n), n)

    def test_plants(self):
        """Растения запрещены наравне с животными: «-N Дерево» проходило."""
        for n in ("Дерево", "Деревья", "Куст", "Цветок", "Кактус",
                  "Пётр Берёзов", "Иван Соснов"):
            self.assertIn("Растение вместо фамилии", titles(n), n)

    def test_plant_like_surnames_not_caught(self):
        """Растение в начале настоящей фамилии — не повод отказывать."""
        for n in ("Пётр Деревенко", "Пётр Верещагин", "Иван Лесничий",
                  "Алекс Кустов"):
            self.assertEqual(blocked(n), [], n)

    def test_roflop(self):
        for n in ("Хрюша Ложкин", "Маньяк Иванов", "Пётр Дураков"):
            self.assertTrue(titles(n), n)

    def test_service_words(self):
        for n in ("admin Иванов", "Алекс Модераторов", "Иван Админ"):
            self.assertTrue(titles(n), n)


class TestProfanity(unittest.TestCase):
    def test_root_forms_caught(self):
        for n in ("Иван Сукачёв", "Пётр Хуйло", "Алекс Бляди", "Иван Долбоёбов",
                  "Пётр Пидор"):
            self.assertIn("Мат", titles(n), n)

    def test_padded_forms_caught(self):
        """«Сукачёв» и «Хуйлов» — мат внутри фамилии."""
        self.assertIn("Мат", titles("Иван Сукачёв"))
        self.assertIn("Мат", titles("Пётр Хуйлов"))


class TestDisguised(unittest.TestCase):
    """Завуалированные слова: склейка даёт аниме-архетип."""

    def test_archetypes(self):
        for n in ("Цун Дере", "Цу Дере", "Куд Ере", "Ян Дере", "Ян Ере",
                  "Дан Дере", "Данд Ере", "Хими Дере", "Хим Дере",
                  "Цун Дере-Арч", "Ян Дере"):
            self.assertIn("Завуалированный аниме-архетип", titles(n), n)

    def test_famous_disguised(self):
        self.assertIn("Похоже на известную личность", titles("Адо Альгит-Лер"))

    def test_famous_plain(self):
        for n in ("Владимир Путин", "Владимир Сталин", "Адольф Гитлер",
                  "Наталья Путина"):
            self.assertIn("Похоже на известную личность", titles(n), n)

    def test_normal_names_not_caught(self):
        for n in ("Алекс Вендетто", "Пётр Стомачёв", "Иван Деревенко",
                  "Алекс Еремеев"):
            self.assertNotIn("Завуалированный аниме-архетип", titles(n), n)


class TestRulesText(unittest.TestCase):
    def test_every_block_has_rule_or_detail(self):
        for name in ("Иван Сукачёв", "Цун Дере", "Лиза Петрова", "Иван Белый"):
            for f in blocked(name):
                self.assertTrue(f.rule or f.detail, f"{name}: {f.title}")

    def test_levels_used(self):
        levels = {f.level for n in ("Алекс Вендетто", "Иван Сукачёв")
                  for f in rpname.check_name(n)}
        self.assertLessEqual(levels, {rpname.BLOCK, rpname.WARN,
                                      rpname.OK, rpname.INFO})


class TestReportShape(unittest.TestCase):
    def test_always_reports_ok_checks(self):
        """Отчёт ценен и когда нарушений нет: видно, что проверено."""
        oks = [f for f in rpname.check_name("Алекс Вендетто") if f.level == "ok"]
        self.assertGreaterEqual(len(oks), 8)

    def test_ok_count_stable(self):
        oks = [f for f in rpname.check_name("Иван Сукачёв") if f.level == "ok"]
        self.assertGreaterEqual(len(oks), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSurnameOnly(unittest.TestCase):
    """Одно слово — это фамилия.

    Сотрудник чаще всего приходит именно с фамилией, и ругаться на неё
    «нет фамилии» бессмысленно. Но имя при этом остаётся непроверенным,
    и отчёт обязан это сказать.
    """

    def test_single_word_is_surname_not_block(self):
        found = rpname.check_name("Воронов")
        self.assertEqual(blocked("Воронов"), [])
        warns = [f for f in found if f.level == "warn"]
        self.assertIn("Введена только фамилия", {f.title for f in warns})

    def test_surname_checks_still_run(self):
        """Одно слово проверяется по всем запретам фамилии."""
        self.assertIn("Мат", titles("Сукачёв"))
        self.assertIn("Цвет вместо фамилии", titles("Белый"))
        self.assertIn("Профессия или должность вместо фамилии", titles("Инженеров"))

    def test_name_not_claimed_as_checked(self):
        """Нельзя писать «имя в полной форме», если имени не было."""
        oks = {f.title for f in rpname.check_name("Воронов")
               if f.level == "ok"}
        self.assertNotIn("Имя в полной форме", oks)
        infos = {f.title for f in rpname.check_name("Воронов")
                 if f.level == "info"}
        self.assertIn("Имя не проверялось", infos)

    def test_full_name_unchanged(self):
        oks = {f.title for f in rpname.check_name("Алекс Вендетто")
               if f.level == "ok"}
        self.assertIn("Имя в полной форме", oks)

    def test_empty_still_blocks(self):
        self.assertIn("Пустое имя", titles("   "))


class TestYoFolding(unittest.TestCase):
    """Слова с «ё» в списках были мёртвыми.

    Проверка нормализует «ё» -> «е», а списки хранили «жёлтый», «пёс»,
    «берёза» как есть. Совпадения не происходили никогда: 27 слов
    лежали без дела, и «-N Дерево» проходило.
    """

    def test_lists_folded(self):
        for w in ("жёлтый", "пёс", "берёза", "ёлка", "чёрт", "верёвка"):
            self.assertIn(w.replace("ё", "е"), rpname.PLANTS_S
                          | rpname.COLORS_S | rpname.ANIMALS_S
                          | rpname.ROFLOP_S | rpname.STUFF_S, w)

    def test_yo_words_now_caught(self):
        # «Пёсов» сюда не входит: «пёс»+«ов» не выводится намеренно,
        # иначе рубили бы настоящие фамилии вроде Пёсцов и Орлов.
        for n in ("Пётр Жёлтый", "Пётр Берёзов", "Пётр Чёртов",
                  "Пётр Клён", "Пётр Орёл"):
            self.assertTrue(blocked(n), n)

    def test_yofold_does_not_overreach(self):
        """После «оживления» списков настоящие фамилии не должны пострадать."""
        for n in ("Пётр Пёсцов", "Пётр Орлов", "Пётр Чертёв",
                  "Пётр Кленов", "Пётр Верёвкин"):
            self.assertEqual(blocked(n), [], n)

    def test_key_of_and_words_agree(self):
        for a, b in (("Ёлка", "Елка"), ("Пёс", "Пес"), ("Чёрт", "Черт")):
            self.assertEqual(rpname.key_of(a), rpname.key_of(b), a)
            self.assertIn(rpname.key_of(a).replace("ё", "е"),
                          rpname._words(a), a)


class TestRuleBeatsReality(unittest.TestCase):
    """Запрещено — значит запрещено, даже если это настоящая фамилия.

    Слова ниже одновременно фамилии и запрещённые по правилам: берёза,
    ёлка, дуб, куст, шахтёр, комбайнёр. Решение владельца: блокировать.
    Тест нужен, чтобы кто-то не счёл их ложными срабатываниями и не
    завёл исключения.
    """

    def test_plant_and_profession_surnames_still_blocked(self):
        for n in ("Пётр Берёзов", "Пётр Елка", "Иван Дуб", "Алекс Куст",
                  "Пётр Шахтёр", "Пётр Комбайнёр"):
            self.assertTrue(blocked(n), n)

    def test_not_reclassified_as_false_positive(self):
        """Слова ловятся тем же правилом, что и обычные запреты."""
        for n in ("Пётр Берёзов", "Пётр Шахтёр"):
            titles_found = titles(n)
            self.assertTrue(
                any("Растение" in t or "Профессия" in t for t in titles_found),
                (n, titles_found))
