import unittest
from datetime import date

from src.integrations.telegram.report_parser import (
    CandidateSelectionStatus,
    normalize_email,
    normalize_phone,
    parse_report,
    parse_report_sections,
)


SOURCE_INN = "1000000001"


class ReportSectionTests(unittest.TestCase):
    def test_parses_only_fields_inside_named_sections(self) -> None:
        sections = parse_report_sections(
            "ignored: value\n"
            "=== Source Alpha ===\n"
            "ИНН: 1000000001\n"
            "Empty:\n\n"
            "=== Source Beta ===\n"
            "Key: value: with colon\n"
        )

        self.assertEqual(
            [section.title for section in sections],
            ["Source Alpha", "Source Beta"],
        )
        self.assertEqual(sections[0].values("инн"), (SOURCE_INN,))
        self.assertEqual(sections[0].values("empty"), ("",))
        self.assertEqual(sections[1].fields[0].value, "value: with colon")


class ContactNormalizationTests(unittest.TestCase):
    def test_normalizes_common_russian_phone_forms(self) -> None:
        self.assertEqual(normalize_phone("8 (900) 111-22-33"), "+79001112233")
        self.assertEqual(normalize_phone("900 111 22 33"), "+79001112233")
        self.assertEqual(normalize_phone("+7 900 111 22 33"), "+79001112233")
        self.assertIsNone(normalize_phone("123"))

    def test_normalizes_only_valid_complete_email(self) -> None:
        self.assertEqual(normalize_email(" User@Example.TEST "), "user@example.test")
        self.assertIsNone(normalize_email("not-an-email"))

    def test_extracts_only_explicit_contact_fields_and_deduplicates(self) -> None:
        report = parse_report(
            "=== Общая сводка ===\n"
            "Телефон: 8 (900) 111-22-33; +7 900 111 22 33\n"
            "Мобильный телефон: +7 900 999 88 77\n"
            "Email: USER@EXAMPLE.TEST\n"
            "E-mail: user@example.test, second@example.test\n"
            "Почта: invalid-address\n"
            "Контактная почта: hidden@example.test\n",
            source_inn=SOURCE_INN,
        )

        self.assertEqual(report.phones, ("+79001112233",))
        self.assertEqual(
            report.emails,
            ("user@example.test", "second@example.test"),
        )

    def test_summary_contacts_are_used_and_foreign_section_is_ignored(self) -> None:
        report = parse_report(
            "=== Общая сводка ===\n"
            "Телефон: +7 900 111-22-33\n"
            "Email: summary@example.test\n\n"
            "=== Связанная организация ===\n"
            "ИНН: 1000000002\n"
            "Телефон: +7 900 999-88-77\n"
            "Email: foreign@example.test\n",
            source_inn=SOURCE_INN,
        )

        self.assertEqual(report.phones, ("+79001112233",))
        self.assertEqual(report.emails, ("summary@example.test",))

    def test_without_summary_only_exact_inn_section_supplies_contacts(self) -> None:
        report = parse_report(
            "=== Requested organization ===\n"
            "ИНН: 1000000001\n"
            "Телефон: +7 900 111-22-33\n"
            "Почта: requested@example.test\n\n"
            "=== Foreign organization ===\n"
            "ИНН: 1000000002\n"
            "Телефон: +7 900 999-88-77\n"
            "Почта: foreign@example.test\n",
            source_inn=SOURCE_INN,
        )

        self.assertEqual(report.phones, ("+79001112233",))
        self.assertEqual(report.emails, ("requested@example.test",))


class PersonCandidateTests(unittest.TestCase):
    def test_candidate_requires_exact_source_inn_in_the_same_section(self) -> None:
        report = parse_report(
            "=== Wrong INN ===\n"
            "ИНН: 1000000002\n"
            "ФИО: Другов Петр Ильич\n"
            "Дата рождения: 02.02.1982\n\n"
            "=== Split identity ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Совпадин Иван Олегович\n\n"
            "=== Split date ===\n"
            "ИНН: 1000000002\n"
            "Дата рождения: 03.03.1983\n",
            source_inn=SOURCE_INN,
        )

        self.assertEqual(report.person_candidates, ())
        self.assertEqual(
            report.candidate_selection.status,
            CandidateSelectionStatus.NONE,
        )

    def test_selects_single_candidate_matching_normalized_director_name(self) -> None:
        report = parse_report(
            "=== Source One ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Соловьёв   Семён Петрович\n"
            "День рождения: 04.05.1984\n\n"
            "=== Source Two ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Иной Алексей Романович\n"
            "Дата рождения: 1980-01-02\n",
            source_inn=SOURCE_INN,
            expected_director_name="соловьев семен петрович",
        )

        selection = report.candidate_selection
        self.assertEqual(selection.status, CandidateSelectionStatus.SELECTED)
        self.assertIsNotNone(selection.candidate)
        self.assertEqual(selection.candidate.date_of_birth, date(1984, 5, 4))
        self.assertFalse(selection.requires_manual_review)

    def test_same_candidate_from_multiple_sections_is_not_ambiguous(self) -> None:
        report = parse_report(
            "=== Source One ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Примеров Павел Андреевич\n"
            "Дата рождения: 06.07.1985\n\n"
            "=== Source Two ===\n"
            "ИНН: 1000000001\n"
            "ФИО: примеров павел андреевич\n"
            "Дата рождения: 06.07.1985\n",
            source_inn=SOURCE_INN,
        )

        self.assertEqual(len(report.person_candidates), 1)
        self.assertEqual(
            report.person_candidates[0].source_sections,
            ("Source One", "Source Two"),
        )
        self.assertEqual(
            report.candidate_selection.status,
            CandidateSelectionStatus.SELECTED,
        )

    def test_multiple_dates_for_matching_name_require_manual_review(self) -> None:
        report = parse_report(
            "=== Source One ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Проверкин Максим Олегович\n"
            "Дата рождения: 01.02.1986\n\n"
            "=== Source Two ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Проверкин Максим Олегович\n"
            "Дата рождения: 02.02.1986\n",
            source_inn=SOURCE_INN,
            expected_director_name="Проверкин Максим Олегович",
        )

        selection = report.candidate_selection
        self.assertEqual(selection.status, CandidateSelectionStatus.AMBIGUOUS)
        self.assertIsNone(selection.candidate)
        self.assertEqual(len(selection.candidates), 2)
        self.assertTrue(selection.requires_manual_review)

    def test_multiple_people_without_expected_director_are_ambiguous(self) -> None:
        report = parse_report(
            "=== Source One ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Первый Антон Олегович\n"
            "Дата рождения: 01.01.1981\n\n"
            "=== Source Two ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Второй Борис Павлович\n"
            "Дата рождения: 02.02.1982\n",
            source_inn=SOURCE_INN,
        )

        selection = report.candidate_selection
        self.assertEqual(selection.status, CandidateSelectionStatus.AMBIGUOUS)
        self.assertIsNone(selection.candidate)
        self.assertTrue(selection.requires_manual_review)

    def test_expected_director_without_match_returns_none(self) -> None:
        report = parse_report(
            "=== Source One ===\n"
            "ИНН: 1000000001\n"
            "ФИО: Найденный Антон Олегович\n"
            "Дата рождения: 01.01.1981\n",
            source_inn=SOURCE_INN,
            expected_director_name="Ожидаемый Борис Павлович",
        )

        self.assertEqual(
            report.candidate_selection.status,
            CandidateSelectionStatus.NONE,
        )
        self.assertIsNone(report.candidate_selection.candidate)

    def test_can_assemble_fio_from_explicit_name_components(self) -> None:
        report = parse_report(
            "=== Component source ===\n"
            "ИНН: 1000000001\n"
            "Фамилия: Составной\n"
            "Имя: Артем\n"
            "Отчество: Игоревич\n"
            "Дата рождения: 08-09-1987\n",
            source_inn=SOURCE_INN,
        )

        candidate = report.candidate_selection.candidate
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.full_name, "Составной Артем Игоревич")
        self.assertEqual(candidate.date_of_birth, date(1987, 9, 8))

    def test_rejects_invalid_source_inn(self) -> None:
        with self.assertRaises(ValueError):
            parse_report("", source_inn="123")


if __name__ == "__main__":
    unittest.main()
