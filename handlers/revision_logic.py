import hashlib
import zlib
import string

from models import User, Token, Repo, HMap, CSet, Blob, CommitMessage
from peewee import IntegrityError, SQL, fn
import RDF
import datetime

import logging
logger = logging.getLogger('debug')

# This factor (among others) determines whether a snapshot is stored rather
# than a delta, depending on the size of the latest snapshot and subsequent
# deltas. For the latest snapshot `base` and deltas `d1`, `d2`, ..., `dn`
# a new snapshot is definitely stored if:
#
# `SNAPF * len(base) <= len(d1) + len(d2) + ... + len(dn)`
#
# In short, larger values will result in longer delta chains and likely reduce
# storage size at the expense of higher revision reconstruction costs.
#
# TODO: Empirically determine a good value with real data/statistics.
SNAPF = 10.0

# Pagination size for indexes (number of resource URIs per page)
INDEX_PAGE_SIZE = 1000

def compress(s):
    return zlib.compress(s)

def decompress(s):
    return zlib.decompress(s)

def __shasum(s):
    return hashlib.sha1(s).digest()

def __get_shasum(key):
    sha = __shasum(key.encode("utf-8")) #hashing
    return sha

'''parse serialized RDF'''
def parse(s, fmt):
    # Parse serialized RDF:
    #
    # RDF/XML:      application/rdf+xml
    # N-Triples:    application/n-triples
    # Turtle:       text/turtle
    stmts = set()
    parser = RDF.Parser(mime_type=fmt)
    for st in parser.parse_string_as_stream(s, "urn:x-default:tailr"):
        stmts.add(str(st) + " .")
    return stmts

def join(parts, sep):
    return string.joinfields(parts, sep)


def __create_hmap_entry(sha, key):
    try:
        HMap.create(sha=sha, val=key)
    except IntegrityError:
        val = (HMap
               .select(HMap.val)
               .where(HMap.sha == sha)
               .scalar())
        if val != key:
            raise IntegrityError

def get_repo(username, reponame):
    try:
        repo = (Repo
            .select()
            .join(User)
            .where((User.name == username) & (Repo.name == reponame))
            .naive()
            .get())
    except Repo.DoesNotExist:
        repo = None
    return repo

def get_chain_at_ts(repo, key, ts):
    sha = __get_shasum(key)
    return __get_chain_at_ts(repo, sha, ts)

def __get_chain_at_ts(repo, sha, ts):
    # Fetch all relevant changes from the last "non-delta" onwards,
    # ordered by time. The returned delta-chain consists of either:
    # a snapshot followed by 0 or more deltas, or
    # a single delete.
    chain = list(CSet
        .select(CSet.time, CSet.type, CSet.len)
        .where(
            (CSet.repo == repo) &
            (CSet.hkey == sha) &
            (CSet.time <= ts) &
            (CSet.time >= SQL(
                "COALESCE((SELECT time FROM cset "
                "WHERE repo_id = %s "
                "AND hkey_id = %s "
                "AND time <= %s "
                "AND type != %s "
                "ORDER BY time DESC "
                "LIMIT 1), 0)",
                repo.id, sha, ts, CSet.DELTA
            )))
        .order_by(CSet.time)
        .naive())
    
    return chain

def get_chain_tail(repo, key):
    sha = __get_shasum(key)
    return __get_chain_tail(repo, sha)

def __get_chain_tail(repo, sha):
    chain = list(CSet
            .select(CSet.time, CSet.type, CSet.len)
            .where(
                (CSet.repo == repo) &
                (CSet.hkey == sha) &
                (CSet.time >= SQL(
                    "COALESCE((SELECT time FROM cset "
                    "WHERE repo_id = %s "
                    "AND hkey_id = %s "
                    "AND type != %s "
                    "ORDER BY time DESC "
                    "LIMIT 1), 0)",
                    repo.id, sha, CSet.DELTA
                )))
            .order_by(CSet.time)
            .naive())

    return chain

def get_chain_last_cset(repo, key):
    sha = __get_shasum(key)
    return __get_chain_last_cset(repo, sha)

def __get_chain_last_cset(repo, sha):
    try:
        last = (CSet
                .select(CSet.time, CSet.type, CSet.len)
                .where(
                    (CSet.repo == repo) &
                    (CSet.hkey == sha))
                .order_by(CSet.time.desc())
                .limit(1)
                .naive()
                .get())
    except CSet.DoesNotExist:
        return None
    return last

def __get_blob_list(repo, sha, chain):
    return list(__get_blobs(repo, sha, chain))

def __get_blobs(repo, sha, chain):
    #logger.info(":: get_blobs")

    blobs = (Blob
        .select(Blob.data)
        .where(
            (Blob.repo == repo) &
            (Blob.hkey == sha) &
            (Blob.time << map(lambda e: e.time, chain)))
        .order_by(Blob.time)
        .naive())
    return blobs

'''get revision as set of statements'''
def get_revision(repo, key, chain):
    #logger.info(":: get_revision")

    sha = __get_shasum(key)
    return __get_revision(repo, sha, chain)

def __get_revision(repo, sha, chain):
    blobs = __get_blobs(repo, sha, chain)
    #if len(chain) == 1:
         # Special case, where we can simply return
         # the blob data of the snapshot.
    #     snap = blobs.first().data
    #     return decompress(snap)

    stmts = set()

    for i, blob in enumerate(blobs.iterator()):
        data = decompress(blob.data)

        if i == 0:
            # Base snapshot for the delta chain
            stmts.update(data.splitlines())
        else:
            for line in data.splitlines():
                mode, stmt = line[0], line[2:]
                if mode == "A":
                    stmts.add(stmt)
                else:
                    stmts.discard(stmt)
    return stmts

def get_csets(repo, key):
    sha = __get_shasum(key)

    # Generate a timemap containing historic change information
    # for the requested key. The timemap is in the default link-format
    # or as JSON (http://mementoweb.org/guide/timemap-json/).
    csets = (CSet
        .select(CSet.time)
        .where((CSet.repo == repo) & (CSet.hkey == sha))
        .order_by(CSet.time.desc())
        .naive())

    return csets

def get_first_cset_of_repo(repo, key):
    sha = __get_shasum(key)

    cset = (CSet
        .select(CSet.time)
        .where((CSet.repo == repo) & (CSet.hkey == sha))
        .order_by(CSet.time)
        .naive()
        .first())
    return cset

def get_last_cset_of_repo(repo, key):
    sha = __get_shasum(key)

    cset = (CSet
        .select(CSet.time)
        .where((CSet.repo == repo) & (CSet.hkey == sha))
        .order_by(CSet.time.desc())
        .naive()
        .first())
    return cset


def get_csets_count(repo, key):
    return get_csets(repo, key).count()

def get_cset_at_ts(repo, key, ts):
    sha = __get_shasum(key)
    return __get_cset_at_ts(repo, sha, ts)

def __get_cset_at_ts(repo, sha, ts):
    try:
        cset = CSet.get(CSet.repo == repo, CSet.hkey == sha, CSet.time == ts)
    except CSet.DoesNotExist:
        return None

    return cset

def get_cset_next_after_ts(repo, key, ts):
    sha = __get_shasum(key)
    return __get_cset_next_after_ts(repo, sha, ts)

def __get_cset_next_after_ts(repo, sha, ts):
    try:
        cset = (CSet
                .select(CSet.time, CSet.type)
                .where((CSet.repo == repo) & (CSet.hkey == sha) & (CSet.time > ts))
                .order_by(CSet.time)
                .limit(1)
                .naive()
                .first())
    except CSet.DoesNotExist:
        return None

    return cset

def get_cset_prev_before_ts(repo, key, ts):
    sha = __get_shasum(key)
    return __get_cset_prev_before_ts(repo, sha, ts)

def __get_cset_prev_before_ts(repo, sha, ts):
    try:
        cset = (CSet
                .select(CSet.time, CSet.type)
                .where((CSet.repo == repo) & (CSet.hkey == sha) & (CSet.time < ts))
                .order_by(CSet.time.desc())
                .limit(1)
                .naive()
                .first())
    except CSet.DoesNotExist:
        return None

    return cset

def get_repo_index(repo, ts, page, limit=None):
    # Subquery for selecting max. time per hkey group
    mx = (CSet
        .select(CSet.hkey, fn.Max(CSet.time).alias("maxtime"))
        .where((CSet.repo == repo) & (CSet.time <= ts))
        .group_by(CSet.hkey)
        .order_by(CSet.hkey)
        .paginate(page, INDEX_PAGE_SIZE)
        .alias("mx"))

    # Query for all the relevant csets (those with max. time values)
    if not limit:
        cs = (CSet
            .select(CSet.hkey, CSet.time)
            .join(mx, on=(
                (CSet.hkey == mx.c.hkey_id) &
                (CSet.time == mx.c.maxtime)))
            .where((CSet.repo == repo) & (CSet.type != CSet.DELETE))
            .alias("cs")
            .naive())
    else:
        cs = (CSet
            .select(CSet.hkey, CSet.time)
            .join(mx, on=(
                (CSet.hkey == mx.c.hkey_id) &
                (CSet.time == mx.c.maxtime)))
            .where((CSet.repo == repo) & (CSet.type != CSet.DELETE))
            .limit(limit)
            .alias("cs")
            .naive())
        
    # Join with the hmap table to retrieve the plain key values
    hm = (HMap
        .select(HMap.val, cs.c.time)
        .join(cs, on=(HMap.sha == cs.c.hkey_id))
        .naive())

    return hm.iterator()


# Is Obsolete. insert_revision() now handles saving revisions (at any time)
# __save_revision() saves revisions with any chain. 
# No usecase for calling save_revision from outside. 
def save_revision(repo, key, stmts, ts):
    sha = __get_shasum(key)
    chain = get_chain_tail(repo, key)

    # TODO transfer this to dedicated function. This is not in the right place here
    if chain == None or len(chain) == 0:
        try:
            __create_hmap_entry(sha, key)
        except IntegrityError:
            raise IntegrityError

    return __save_revision(repo, sha, chain, stmts, ts)

def __save_revision(repo, sha, chain, stmts, ts):
    # this checks if timestamp is after the last cset of the chain, not if its after all csets for key.
    # this allows pushing to any timestamp, if the chain is right
    if chain and len(chain) > 0 and not ts > chain[-1].time:
        # Appended timestamps must be monotonically increasing!
        raise ValueError

    if len(chain) == 0 or chain[0].type == CSet.DELETE:
        # Provide dummy value for `patch` which is never stored.
        # If we get here, we always store a snapshot later on!
        patch = ""
    else:
        # Reconstruct the previous state of the resource
        prev = __get_revision(repo, sha, chain)

        if stmts == prev:
            # No changes, nothing to be done. Bail out.
            return None

        patch = compress(join(
            map(lambda s: "D " + s, prev - stmts) +
            map(lambda s: "A " + s, stmts - prev), "\n"))

    snapc = compress(join(stmts, "\n"))

    # Calculate the accumulated size of the delta chain including
    # the (potential) patch from the previous to the pushed state.
    accumulated_len = reduce(lambda s, e: s + e.len, chain[1:], 0) + len(patch)

    base_len = len(chain) > 0 and chain[0].len or 0 # base length

    if (len(chain) == 0 or
        chain[0].type == CSet.DELETE or
        len(snapc) <= len(patch) or
        SNAPF * base_len <= accumulated_len):
        # Store the current state as a new snapshot
        Blob.create(repo=repo, hkey=sha, time=ts, data=snapc)
        CSet.create(repo=repo, hkey=sha, time=ts, type=CSet.SNAPSHOT,
            len=len(snapc))
    else:
        # Store a directed delta between the previous and current state
        Blob.create(repo=repo, hkey=sha, time=ts, data=patch)
        CSet.create(repo=repo, hkey=sha, time=ts, type=CSet.DELTA,
            len=len(patch))
    return 0

def save_revision_delete(repo, key, ts):
    sha = __get_shasum(key)
    return __save_revision_delete(repo, sha, ts)

def __save_revision_delete(repo, sha, ts):
    chain = __get_chain_at_ts(repo, sha, ts)
    if chain[-1]:
        if not chain[-1].type == CSet.DELETE:
            # only if there are csets before and the last is no delete
            stmts_next = set()
            cset_next = __get_cset_next_after_ts(repo, sha, ts)
            if cset_next != None:
                if cset_next.type == CSet.DELTA:
                    # If next changeset is Delta, keep next revision statements
                    chain_next = __get_chain_at_ts(repo, sha, cset_next.time)
                    stmts_next = __get_revision(repo, sha, chain_next)
                elif cset_next.type == CSet.DELETE:
                    # If next changeset is Delete, remove it
                    __remove_cset(repo, sha, cset_next.time)
                # Nothing to be done if next changeset is a Snapshot

                if __get_cset_at_ts(repo, sha, ts):
                    # If there is a cset at the exact time, delete it 
                    # (reconstruction of next Cset already happened)
                    __remove_revision(repo, sha, ts) 

            # Insert the new "delete" change
            CSet.create(repo=repo, hkey=sha, time=ts, type=CSet.DELETE, len=0)

            if cset_next != None:
                if cset_next.type == CSet.DELTA:
                    # If next changeset is Delta, reconstruct
                    __remove_cset(repo, sha, cset_next.time)
                    chain = __get_chain_at_ts(repo, sha, cset_next.time)
                    __save_revision(repo, sha, chain, stmts_next, cset_next.time)
        else:
            # Nothing happens. Resource is already deleted. This is legitimate, so no exception needed
            # TODO should this be announced to client somehow?
            pass
    else:
        raise LookupError

def insert_revision(repo, key, stmts, ts):
    sha = __get_shasum(key)
    return __insert_revision(repo, key, sha, stmts, ts)

def __insert_revision(repo, key, sha, stmts, ts):
    # keep next revision statements
    stmts_next = set()
    cset_next = __get_cset_next_after_ts(repo, sha, ts)

    if cset_next != None:
        if cset_next.type == CSet.DELTA or cset_next.type == CSet.SNAPSHOT:
            chain_next = __get_chain_at_ts(repo, sha, cset_next.time)
            stmts_next = __get_revision(repo, sha, chain_next)
            # If next cset is Delete, no need to reconstruct 

        if __get_cset_at_ts(repo, sha, ts):
            # check if there is a revision at this ts. If so remove it for replacement
            # (reconstruction of next Cset already happened)
            __remove_revision(repo, sha, ts)
        
        chain_current = __get_chain_at_ts(repo, sha, ts)
        # save inserted revision
        return __save_revision(repo, sha, chain_current, stmts, ts)

        if cset_next.type == CSet.DELTA or cset_next.type == CSet.SNAPSHOT:
            # delete next revision
            __remove_cset(repo, sha, cset_next.time)
            # reconstruct next revision
            chain = __get_chain_at_ts(repo, sha, cset_next.time)
            return __save_revision(repo, sha, chain, stmts_next, cset_next.time)
    else:
        if __get_cset_at_ts(repo, sha, ts):
            # check if there is a revision at this ts, which is the latest one. 
            # If so remove it for replacement (no reconstruction needed, as there is no next Cset)
            __remove_revision(repo, sha, ts)
        
        chain_current = __get_chain_at_ts(repo, sha, ts)

        # check if the resource exists
        if chain_current == None or len(chain_current) == 0:
            try:
                __create_hmap_entry(sha, key)
            except IntegrityError:
                raise IntegrityError

        # If there is no cset following, just save the new statements
        return __save_revision(repo, sha, chain_current, stmts, ts)


def add_commit_message(repo, key, ts, message):
    sha = __get_shasum(key)
    return __add_commit_message(repo, sha, ts, message)

def __add_commit_message(repo, sha, ts, message):

    # TODO sanitize message

    CommitMessage.create(repo=repo, hkey=sha, time=ts, message=message)

def get_commit_message(repo, key, ts):
    sha = __get_shasum(key)
    return __get_commit_message(repo, sha, ts)

def __get_commit_message(repo, sha, ts):
    try:
        commit_message = CommitMessage.get(CommitMessage.repo == repo, CommitMessage.hkey == sha, CommitMessage.time == ts)
    except CommitMessage.DoesNotExist:
        return None
    return commit_message.message

# Whether replacement is needed is checked in __insert_revision() so
# this one is not used right now but could become handy in the future
def replace_revision(repo, key, stmts, ts):
    sha = __get_shasum(key)
    return __replace_revision(repo, sha, stmts, ts)

def __replace_revision(repo, sha, stmts, ts):
    if __get_cset_at_ts(repo, sha, ts):
        __remove_revision(repo, sha, ts)
        __insert_revision(repo, sha, stmts, ts)
    

def get_delta_of_memento(repo, key, ts):
    sha = __get_shasum(key)
    return __get_delta_of_memento(repo, sha, ts)

def __get_delta_of_memento(repo, sha, ts):
    added = set()
    deleted = set()

    chain = __get_chain_at_ts(repo, sha, ts)

    if len(chain) > 0:
        cset = chain[-1]
        if cset.type == CSet.DELETE:
            # everything was deleted and is a delta here
            # get the chain before the delete, therefore decrease timestamp of current memento
            prev_chain = __get_chain_at_ts(repo, sha, cset.time - datetime.timedelta(seconds=1))
            if len(prev_chain) > 0:
                prev_data = __get_revision(repo, sha, prev_chain)
                deleted = map(lambda s: "" + s, prev_data)
        elif cset.type == CSet.DELTA:
            # If Memento is a delta, we just need to deliver the delta itself
            data = decompress(__get_blob_list(repo, sha, chain)[-1].data)
            for line in data.splitlines():
                if line[0] == "A":
                    # cutoff 'A '
                    added.add(line[2:])
                else:
                    # cutoff 'D '
                    deleted.add(line[2:])
        else:
            # CSet is Snapshot => Calculate Delta from snapshot to last delta
            current_data = __get_revision(repo, sha, chain)
            # get the chain before the snashot, therefore decrease timestamp of current memento
            prev_chain = __get_chain_at_ts(repo, sha, cset.time - datetime.timedelta(seconds=1))
            if len(prev_chain) > 0:
                prev_data = __get_revision(repo, sha, prev_chain)
                added = map(lambda s: "" + s, current_data - prev_data)
                deleted = map(lambda s: "" + s, prev_data - current_data)
            else:
                # No Memento before this snapshot, everything was added
                added = map(lambda s: "" + s, current_data)

    return added, deleted

def get_delta_between_mementos(repo, key, ts, delta_ts):
    sha = __get_shasum(key)
    return __get_delta_between_mementos(repo, sha, ts, delta_ts)

def __get_delta_between_mementos(repo, sha, ts, delta_ts):
    added = set()
    deleted = set()

    chain = __get_chain_at_ts(repo, sha, ts)
    prev_chain = __get_chain_at_ts(repo, sha, delta_ts)

    if len(chain) > 0 and len(prev_chain) > 0:
        data = __get_revision(repo, sha, chain)
        prev_data = __get_revision(repo, sha, prev_chain)
        added = map(lambda s: "A " + s, data - prev_data)
        deleted = map(lambda s: "D " + s, prev_data - data)
    else:
        # In any other case at least at one time there is no resource. 
        raise ValueError
    return added, deleted



#### Repository management ####

def remove_repo(repo):
    # remove all csets
    __remove_csets_repo(repo)
    # remove keys from hmap
    __cleanup_hmap()
    # remove repo
    __remove_repo(repo)

def __cleanup_hmap():
    # TODO delete all entries whose sha is not referenced in CSet any more
    pass

def __remove_repo(repo):
    # remove repo
    repo.delete_instance(recursive=True)

def __remove_csets_repo(repo):
    # remove blobs
    q_blobs = Blob.delete().where(Blob.repo == repo)
    q_blobs.execute()

    # remove csets
    q_csets = CSet.delete().where(CSet.repo == repo)
    q_csets.execute()

def __remove_csets(repo, sha):
    # remove blobs
    q_blobs = Blob.delete().where(Blob.repo == repo, Blob.hkey == sha)
    q_blobs.execute()

    # remove csets
    q_csets = CSet.delete().where(CSet.repo == repo, CSet.hkey == sha)
    q_csets.execute()

def __remove_cset(repo, sha, ts):
    # remove cset
    try:
        cset = CSet.get(CSet.repo == repo, CSet.hkey == sha, CSet.time == ts)
        count = cset.delete_instance()
    except CSet.DoesNotExist:
        return None
    
    # remove blob
    try:
        blob = Blob.get(Blob.repo == repo, Blob.hkey == sha, Blob.time == ts)
        blob.delete_instance()
    except Blob.DoesNotExist:
        return None


def remove_revision(repo, key, ts):
    # (repo, hkey, time) is composite key for cset
    sha = __get_shasum(key)
    return __remove_revision(repo, sha, ts)

def __remove_revision(repo, sha, ts):
    # keep next revision statements
    stmts_next = set()
    cset_next = __get_cset_next_after_ts(repo, sha, ts)
    if cset_next != None:
        # also recalculate snapshots in case that snapshot equals the last cset
        if cset_next.type == CSet.DELTA or cset_next.type == CSet.SNAPSHOT:
            # not last cset
            chain_next = __get_chain_at_ts(repo, sha, cset_next.time)
            stmts_next = __get_revision(repo, sha, chain_next)

    # remove blob and cset
    __remove_cset(repo, sha, ts)



    # if not last cset, re-compute next cset
    if cset_next != None:
        if cset_next.type == CSet.DELTA or cset_next.type == CSet.SNAPSHOT:
            __remove_cset(repo, sha, cset_next.time)
            chain = __get_chain_at_ts(repo, sha, cset_next.time)
            __save_revision(repo, sha, chain, stmts_next, cset_next.time)

    # if all csets removed, remove key from hmap
    if (cset_next == None):
        __cleanup_hmap()
