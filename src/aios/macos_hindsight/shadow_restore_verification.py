"""Compare a PostgreSQL dump and restore without exposing row contents."""
from contextlib import contextmanager
import re


def _quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def sequence_state(connection, names):
    result = {}
    for schema, name in names:
        relation = _quote_identifier(schema) + '.' + _quote_identifier(name)
        [values] = connection.query(f'SELECT last_value::text, is_called::text FROM {relation}')
        result[(schema, name)] = tuple(values)
    return result


@contextmanager
def read_only_snapshot(connection):
    connection.query('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
    try:
        connection.query("SET LOCAL search_path = pg_catalog; SET LOCAL statement_timeout = '30s'; "
                         "SET LOCAL lock_timeout = '5s'; SET LOCAL TimeZone TO 'UTC'; "
                         "SET LOCAL extra_float_digits TO 3; SET LOCAL DateStyle TO 'ISO, YMD'; "
                         "SET LOCAL IntervalStyle TO 'postgres'")
        snapshot = connection.scalar('SELECT pg_export_snapshot()')
        if not re.fullmatch(r'[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9A-Fa-f]+', snapshot):
            raise ValueError('invalid PostgreSQL snapshot identifier')
        yield snapshot
    finally:
        connection.query('ROLLBACK')


def fingerprint(connection, schemas):
    if connection.scalar("SELECT current_setting('transaction_read_only')") != 'on':
        raise ValueError('fingerprint requires a read-only transaction')
    if not schemas or len(schemas) != len(set(schemas)) or any(
        not re.fullmatch(r'[a-z][a-z0-9_]*', name) for name in schemas
    ):
        raise ValueError('invalid reviewed schemas')
    filter_sql = '(' + ', '.join(connection.literal(schema) for schema in schemas) + ')'
    relation_filter = f'n.nspname IN {filter_sql}'
    relations = connection.query(
        'SELECT n.nspname,c.relname,c.relkind FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid '
        f"WHERE {relation_filter} AND c.relkind IN ('r','p','v','m','f','S') ORDER BY 1,2")
    if not relations or set(schemas) - {row[0] for row in relations}:
        raise ValueError('reviewed schema or relations missing')
    if any(row[2] in ('p', 'm', 'f') for row in relations):
        raise ValueError('relation requires a separate consistency check')
    rows = {}
    for schema, name, kind in relations:
        if kind != 'r':
            continue
        target = _quote_identifier(schema) + '.' + _quote_identifier(name)
        [values] = connection.query(
            "SELECT count(*)::text, encode(sha256(convert_to(coalesce(string_agg(row_hash, '' "
            "ORDER BY row_hash COLLATE \"C\"), ''), 'UTF8')), 'hex') FROM ("
            "SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') AS row_hash "
            f'FROM ONLY {target} AS t) AS hashes')
        rows[(schema, name)] = tuple(values)
    return {
        'rows': rows,
        'sequence_state': sequence_state(connection, ((schema, name) for schema, name, kind in relations if kind == 'S')),
        'sequence_configuration': connection.query(
            'SELECT n.nspname,c.relname,pg_get_userbyid(c.relowner),format_type(s.seqtypid, NULL), '
            's.seqstart::text,s.seqincrement::text,s.seqmin::text,s.seqmax::text,s.seqcache::text,s.seqcycle::text '
            'FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid '
            f'JOIN pg_sequence s ON s.seqrelid=c.oid WHERE {relation_filter} ORDER BY 1,2'),
        'relations': connection.query(f'SELECT n.nspname,c.relname,c.relkind,pg_get_userbyid(c.relowner),'
                                      ' c.relrowsecurity::text, c.relforcerowsecurity::text '
                                      'FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid '
                                      f"WHERE {relation_filter} AND c.relkind IN ('r','v') ORDER BY 1,2"),
        'grants': connection.query(
            "SELECT n.nspname,c.relname,pg_get_userbyid(a.grantor),"
            "CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,"
            'a.privilege_type,a.is_grantable::text FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid '
            "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault("
            "CASE WHEN c.relkind='S' THEN 's'::\"char\" ELSE 'r'::\"char\" END,c.relowner))) AS a "
            f"WHERE {relation_filter} AND c.relkind IN ('r','v','S') ORDER BY 1,2,3,4,5,6"),
        'schema_grants': connection.query(
            "SELECT n.nspname,pg_get_userbyid(n.nspowner),pg_get_userbyid(a.grantor),"
            "CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,"
            'a.privilege_type,a.is_grantable::text FROM pg_namespace n '
            "CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) AS a "
            f'WHERE {relation_filter} ORDER BY 1,2,3,4,5,6'),
        'column_shape': connection.query(
            'SELECT n.nspname,c.relname,a.attname,format_type(a.atttypid,a.atttypmod), a.attnotnull::text '
            'FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid '
            'JOIN pg_attribute a ON a.attrelid=c.oid '
            f"WHERE {relation_filter} AND c.relkind IN ('r','v') AND a.attnum>0 AND NOT a.attisdropped "
            'ORDER BY 1,2,a.attnum'),
        'indexes': connection.query('SELECT n.nspname,c.relname,pg_get_indexdef(i.indexrelid) '
                                    'FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid '
                                    'JOIN pg_index i ON i.indrelid=c.oid '
                                    f'WHERE {relation_filter} ORDER BY 1,2,3'),
        'extensions': connection.query('SELECT name, version, nspname FROM '
                                       '(SELECT e.extname AS name,e.extversion AS version,n.nspname '
                                       'FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace) AS extensions '
                                       'ORDER BY 1'),
    }


def compare(source, restored):
    return {
        'all_fingerprints_match': source == restored,
        'table_count': len(source['rows']),
        'sequence_count': len(source['sequence_state']),
        'row_count': sum(int(entry[0]) for entry in source['rows'].values()),
        'mismatched_sections': sorted(key for key in source if source[key] != restored[key]),
        'mismatched_table_count': sum(source['rows'].get(k) != restored['rows'].get(k)
                                      for k in source['rows'].keys() | restored['rows'].keys()),
    }
