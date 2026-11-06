"""Bounded exact body-token retrieval observation; no write or repair."""
from __future__ import annotations
import hashlib
import re

PATH=re.compile(r'Memory/notes/(selective-[0-9a-f]{64})\.md\Z')
SHA=re.compile(r'[0-9a-f]{64}\Z')
def valid_marker(value):
    # Keep existing ASCII minimum; allow Japanese and other Unicode letters/numbers.
    if not isinstance(value, str) or not 2 <= len(value) <= 100:
        return False
    if value.isascii() and len(value) < 4:
        return False
    return value[0].isalnum() and all(c.isalnum() or c in '_-' for c in value)



def observe(path, revision, marker, *, search, read):
    base={'ready':False,'index_coverage':'unknown','atomic_snapshot':False,
          'scope':'one_body_literal_search_and_full_source_read'}
    if not isinstance(path,str) or not PATH.fullmatch(path) or not isinstance(revision,str) or not SHA.fullmatch(revision):
        return {**base,'reason':'invalid_target'}
    stem=PATH.fullmatch(path).group(1)
    if not valid_marker(marker) or marker.casefold() in stem.casefold():
        return {**base,'reason':'invalid_marker'}
    try:
        found=search(marker)
        if not isinstance(found,dict) or not isinstance(found.get('results'),list) or len(found['results'])>100:
            return {**base,'reason':'search_unconfirmed'}
        rows=found['results']
        total,page,size,more=found.get('total'),found.get('current_page'),found.get('page_size'),found.get('has_more')
        if (type(total) is not int or total<len(rows) or found.get('total_is_exact') is not True
                or type(page) is not int or page!=1 or type(size) is not int or not 1<=size<=100
                or len(rows)>size or type(more) is not bool or more!=(total>len(rows))
                or (more and len(rows)!=size)):
            return {**base,'reason':'search_unconfirmed'}
        hits=[x for x in rows if isinstance(x,dict) and x.get('read_path')==path and x.get('file_path')=='notes/'+stem+'.md' and x.get('title')==stem and x.get('type')=='entity']
        if len(hits)!=1:
            return {**base,'reason':'search_unconfirmed'}
        source=read(path)
        if not isinstance(source,dict) or source.get('path')!=path or source.get('revision')!=revision or not isinstance(source.get('text'),str):
            return {**base,'reason':'source_unconfirmed'}
        text=source['text']
        if len(text.encode('utf8'))>131072 or hashlib.sha256(text.encode('utf8')).hexdigest()!=revision:
            return {**base,'reason':'source_unconfirmed'}
        content=text.partition('<!-- aios-selective-memory-v1 -->')
        if not content[1] or marker.casefold() not in content[2].casefold():
            return {**base,'reason':'marker_not_in_source'}
        return {**base,'ready':True,'reason':None}
    except Exception:
        return {**base,'reason':'observation_unavailable'}
