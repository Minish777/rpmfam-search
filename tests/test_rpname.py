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

    def test_no_surname(self):
        self.assertIn("Нет фамилии", titles("Алекс"))

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
