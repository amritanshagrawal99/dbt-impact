from dbt_impact.lineage import analyse_model

UP = {"db.s.orders": ["order_id", "customer_id", "status", "amount"], "db.s.customers": ["customer_id", "name", "email"]}


def facts(sql):
    return analyse_model(sql, UP, "duckdb")


def test_explicit_refs_from_select_join_where_group():
    f = facts("""select c.name, sum(o.amount) as total
                 from db.s.orders o join db.s.customers c on c.customer_id = o.customer_id
                 where o.status = 'done' group by c.name""")
    assert f.explicit["db.s.orders"] == {"amount", "customer_id", "status"}
    assert f.explicit["db.s.customers"] == {"customer_id", "name"}
    assert f.outputs == ["name", "total"]
    assert not f.star_from


def test_alias_star_is_a_star_reference_not_explicit():
    f = facts("select o.*, 1 as one from db.s.orders o")
    assert "db.s.orders" in f.star_from
    assert "db.s.orders" not in f.explicit
    assert f.outputs == ["order_id", "customer_id", "status", "amount", "one"]


def test_star_through_cte_reaches_the_table():
    f = facts("with x as (select * from db.s.orders) select * from x")
    assert f.star_from == {"db.s.orders"}


def test_column_read_through_star_cte_is_explicit_on_the_table():
    f = facts("with x as (select * from db.s.orders) select status from x")
    assert f.explicit["db.s.orders"] == {"status"}
    assert not f.star_from


def test_column_named_in_cte_select_is_attributed_inside_the_cte():
    f = facts("with x as (select order_id, status as st from db.s.orders) select st from x")
    assert f.explicit["db.s.orders"] == {"order_id", "status"}


def test_unqualified_unknown_column_single_source():
    f = analyse_model("select id, gone_col from db.s.orders", {"db.s.orders": ["id"]}, "duckdb")
    assert "gone_col" in f.explicit["db.s.orders"]


def test_unknown_column_in_join_falls_back_without_schema():
    f = facts("select o.order_id, c.first_name from db.s.orders o join db.s.customers c on c.customer_id = o.customer_id")
    assert "first_name" in f.explicit["db.s.customers"]
    assert f.error is None or "first_name" in f.error


def test_plain_renames_recorded():
    f = facts("select order_id as id, upper(status) as status_u from db.s.orders")
    assert f.aliases == {"id": "order_id"}


def test_unparseable_sql_is_opaque_not_fatal():
    f = facts("select from where ((")
    assert f.outputs is None and f.error
