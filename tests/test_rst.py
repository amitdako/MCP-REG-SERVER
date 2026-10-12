"""RST parsing: heading detection, cleanup, includes.

These are the cases where a wrong guess silently ruins retrieval: a table border
read as a heading splits a table in half, an unresolved include loses every
config parameter, markup noise pollutes both embeddings and keyword search.
"""

from __future__ import annotations

import logging
from textwrap import dedent

from rag_mcp import rst
from rag_mcp.document import document_title, parse_document


def sections(text: str):
    _, secs = rst.split_sections(dedent(text))
    return [(s.level, s.title) for s in secs]


# ---------------------------------------------------------------- headings

def test_levels_follow_order_of_first_appearance():
    text = """
    Page title
    ==========

    Intro.

    Section
    -------

    Body.

    Subsection
    ~~~~~~~~~~

    Deep.

    Another section
    ---------------

    More.
    """
    assert sections(text) == [(1, "Page title"), (2, "Section"), (3, "Subsection"), (2, "Another section")]


def test_overline_style_is_distinct_from_underline_only():
    text = """
    ======
    Title
    ======

    Section
    =======

    text
    """
    assert sections(text) == [(1, "Title"), (2, "Section")]


def test_short_sloppy_underline_still_counts():
    # Real docs have underlines shorter than the title.
    assert sections("``kafka``\n----\n\n*boolean*\n") == [(1, "kafka")]


def test_table_borders_are_not_headings():
    text = """
    Limits
    ======

    +---------------------------+-------+
    | Parameter                 | Value |
    +===========================+=======+
    |``tcp_keepalives_idle``    |180    |
    +---------------------------+-------+

    =====  =====
    A      B
    =====  =====
    1      2
    =====  =====
    """
    preamble, secs = rst.split_sections(dedent(text))
    assert [s.title for s in secs] == ["Limits"]
    body = secs[0].body
    assert "+---" not in body and "=====" not in body  # decoration dropped
    assert "`tcp_keepalives_idle` |180 |" in body  # cell text kept


def test_paragraph_followed_by_transition_is_not_a_heading():
    text = """
    Title
    =====

    first line of a paragraph
    second line
    ----------

    after
    """
    assert sections(text) == [(1, "Title")]


# ---------------------------------------------------------------- inline + blocks

def test_inline_markup_becomes_readable_text():
    clean = rst.clean_inline(
        "See :doc:`the Kafka guide </docs/products/kafka>` and `the site <https://aiven.io>`_. "
        "Set ``max_connections``. :bdg-secondary:`PG10 only` :ref:`plain-ref`",
        {},
    )
    assert clean == "See the Kafka guide and the site. Set `max_connections`. PG10 only plain-ref"


def test_body_keeps_facts_and_drops_noise():
    body = dedent("""
        .. _some-label:

        .. image:: /images/diagram.png
           :alt: diagram

        .. toctree::
           :hidden:

           page-one

        .. this is a comment
           spanning two lines

        .. note::
           The limit is **100** topics.

        .. code-block:: bash

            avn service create --plan startup-4

        Run this::

            psql "$SERVICE_URI"

        .. list-table:: Plans
          :header-rows: 1
          :widths: 15 60

          * - Plan
            - Max connections
          * - Startup-4
            - 100
        """)
    out = rst.clean_body(body, {})
    for noise in ("some-label", "diagram", "page-one", "comment", ":header-rows:", ":widths:"):
        assert noise not in out
    assert "Note:\nThe limit is **100** topics." in out
    assert "```bash\navn service create --plan startup-4\n```" in out
    assert 'Run this:\n```\npsql "$SERVICE_URI"\n```' in out
    assert "Plans\n- Plan: Startup-4 · Max connections: 100" in out


def test_list_table_rows_become_self_contained_lines():
    body = dedent("""
        .. list-table::
          :header-rows: 1

          * - Parameter
            - Value Type
            - Description
          * - ``admin_username``
            - string
            - Custom username for admin user.

              The default value is: ``avnadmin``
          * - ``--remove-option migration``
            -
            - Removes the migration option.
              Nested list:

              - first
              - second
        """)
    assert rst.clean_body(body, {}).splitlines() == [
        "- Parameter: `admin_username` · Value Type: string · Description: Custom username "
        "for admin user. The default value is: `avnadmin`",
        # empty cell skipped; a deeper "- " is part of the cell, not a new column
        "- Parameter: `--remove-option migration` · Description: Removes the migration option. "
        "Nested list: - first - second",
    ]


def test_list_table_without_header_rows_keeps_plain_cells():
    body = ".. list-table::\n\n  * - a\n    - b\n  * - c\n    - d\n"
    assert rst.clean_body(body, {}).splitlines() == ["- a · b", "- c · d"]


def test_half_literal_typo_is_repaired_but_links_are_not_paired_with_literals():
    assert rst.clean_inline("The default value is: `top``", {}) == "The default value is: `top`"
    # The `_ closing a link must not pair with the next real ``literal`` on the line.
    line = "For `Coralogix <https://coralogix.com/>`_ integration use a custom ``logline`` format"
    assert rst.clean_inline(line, {}) == "For Coralogix integration use a custom `logline` format"


def test_links_and_roles_wrapped_across_lines_are_cleaned():
    body = dedent("""
        Another option is to use the `Aiven
        Client <https://github.com/aiven/aiven-client>`__ . Consider :doc:`granting
        </docs/products/clickhouse/howto/manage-users-roles>` only what is needed.

        .. code-block:: rst

            keep `this
            <https://example.com>`_ verbatim
        """)
    out = rst.clean_body(body, {})
    assert "use the Aiven Client . Consider granting only what is needed." in out.replace("\n", " ")
    assert "github.com" not in out and "manage-users-roles" not in out
    assert "keep `this\n<https://example.com>`_ verbatim" in out


def test_code_is_kept_verbatim():
    # Inline cleanup must not touch code: ``x`` and roles inside code are literal.
    body = ".. code:: python\n\n    x = '``not markup``'\n"
    assert "x = '``not markup``'" in rst.clean_body(body, {})


def test_substitutions_are_resolved():
    text = """
    .. |max_conn| replace:: ``max_connections``

    Title
    =====

    Use |max_conn| here. A table cell | stays |.
    """
    _, secs = rst.split_sections(dedent(text))
    assert "Use `max_connections` here. A table cell | stays |." in secs[0].body


# ---------------------------------------------------------------- includes

def test_includes_are_inlined_absolute_relative_and_nested(tmp_path):
    (tmp_path / "includes").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "includes" / "config.rst").write_text("``ip_filter``\n-------------\n\nCIDR list.\n\n.. include:: nested.rst\n")
    (tmp_path / "includes" / "nested.rst").write_text("Nested text.\n")
    (tmp_path / "docs" / "sibling.rst").write_text("Sibling text.\n")
    page = tmp_path / "docs" / "page.rst"
    text = "Page\n====\n\n.. include:: /includes/config.rst\n\n.. include:: sibling.rst\n"

    resolved = rst.resolve_includes(text, page, tmp_path)

    assert "CIDR list." in resolved and "Nested text." in resolved and "Sibling text." in resolved
    assert ".. include::" not in resolved


def test_missing_include_is_dropped_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        resolved = rst.resolve_includes(".. include:: /nope.rst\nkept\n", tmp_path / "p.rst", tmp_path)
    assert resolved == "kept"
    assert "nope.rst" in caplog.text


def test_include_cycle_terminates(tmp_path):
    (tmp_path / "a.rst").write_text("A\n\n.. include:: a.rst\n")
    resolved = rst.resolve_includes((tmp_path / "a.rst").read_text(), tmp_path / "a.rst", tmp_path)
    assert resolved.count("A") <= 10


def test_config_parameters_get_disambiguating_paths(tmp_path):
    # Mirrors devportal: the page is one include; `kafka` exists under two parents.
    (tmp_path / "includes").mkdir()
    (tmp_path / "includes" / "config-kafka.rst").write_text(dedent("""
        ``private_access``
        ------------------
        *object*

        ``kafka``
        ~~~~~~~~~
        *boolean*

        Private DNS name.

        ``public_access``
        -----------------
        *object*

        ``kafka``
        ~~~~~~~~~
        *boolean*

        Public internet access.
        """))
    page = tmp_path / "advanced-params.rst"
    page.write_text("Advanced parameters for Kafka\n=============================\n\n.. include:: /includes/config-kafka.rst\n")

    root = parse_document(rst.resolve_includes(page.read_text(), page, tmp_path), "rst")

    paths = {n.path: n.body for n in root.walk() if n.title}
    assert document_title(root) == "Advanced parameters for Kafka"
    assert "Private DNS name." in paths[("Advanced parameters for Kafka", "private_access", "kafka")]
    assert "Public internet access." in paths[("Advanced parameters for Kafka", "public_access", "kafka")]
