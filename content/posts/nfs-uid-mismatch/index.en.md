---
title: "One User, Two UIDs: NFSv4, Kerberos, and Docker on a Domain-Joined TrueNAS"
date: 2026-10-13
url: "/posts/nfs-uid-mismatch-en/"
translationKey: "nfs-uid-mismatch"
description: "SSSD and TrueNAS gave the same AD user different UIDs. How that showed up as nobody, Permission denied, root:root 755, and \"I have no name!\", and how we aligned them."
tags: ["docker", "nfs", "kerberos", "truenas", "active-directory"]
ShowToc: true
---

## Introduction

On a set of shared, multi-user Linux workstations, several things broke in the same few weeks. Files on a user's NFS home showed up as `nobody:nogroup`. Once we tightened the share, a user could no longer write to their own home, even though `ls` showed them as the owner. A freshly provisioned home dataset came out owned by `root:root` with mode `755` instead of belonging to the user with mode `700`. A user's Jupyter container opened with the prompt `I have no name!` and `sudo` refused to work.

Most of this came from one root cause. The same Active Directory user had one UID on the Linux clients and a different UID on the storage server. Neither side was misconfigured on its own terms. In the end, we computed the base the clients used and set the storage server's ID range to start at the same number. Two more lookups on the same path, one on the client and one on the server, also had to be fixed before access worked.

This post follows one identity from login to an NFS file, shows where the mismatch hurts, and covers the fix, a checklist, and alternatives.

## The Setup

The environment has four parts:

- **Domain controller:** a Samba-based AD appliance for the `EXAMPLE.LOCAL` realm.
- **Storage:** TrueNAS SCALE at `nas.example.local` (192.0.2.10), joined to the same domain. Every user gets a separate ZFS dataset, exported over NFSv4 with `sec=krb5` only. Each dataset is owned by its user with mode `0700`. The owner has full access and everyone else has none.
- **Clients:** Ubuntu workstations joined to the domain with SSSD. `pam_mount` mounts the user's dataset at login, and `rpc.gssd` handles the Kerberos side of NFS.
- **Docker:** users run their work (Jupyter, training jobs) in containers. The user's NFS directory is bind-mounted into the container as `/workspace`.

The isolation goal is simple: user A must not be able to read user B's dataset, even with Docker in the picture.

The diagram shows which identity is in effect at each hop, before the fix. The UIDs are examples.

```mermaid
flowchart TD
    A["Login on client<br/>AD password"] --> B["SSSD<br/>SID to UID (ID mapping)<br/>alice = 1914801105"]
    B --> C["SSSD krb5_child<br/>TGT in /tmp/krb5cc_1914801105"]
    C --> D["pam_mount<br/>mount nas:/.../alice sec=krb5"]
    D --> E["docker exec --user 1914801105<br/>process in container"]
    E --> F["Kernel NFS client<br/>caller UID = 1914801105"]
    F --> G["rpc.gssd<br/>finds ccache by UID<br/>principal alice@EXAMPLE.LOCAL"]
    G --> H["TrueNAS<br/>principal to UID via its own idmap (RID)<br/>alice = 100001106"]
    H --> I["ZFS permission check<br/>owner 100001106, mode 0700"]
    I --> J["Reply: owner as name string<br/>EXAMPLE\\alice@example.local"]
    J --> K["Client nfsidmap + SSSD<br/>name to local UID 1914801105"]
```

The client's number never reaches the server; it only selects the Kerberos ticket. The server uses the principal and its own number, and the owner comes back as a name. The two numbers never meet, which is why the mismatch could hide.

## Why Containers Must Not Run as Root Here

With `sec=sys`, the server trusts the UID the client sends. With `sec=krb5`, it trusts a Kerberos principal, and the client picks that principal from the UID of the calling process.

### How the client picks a Kerberos identity

Per the `rpc.gssd` man page, user credentials live in a file with "a well-known name constructed using the user's UID", typically in `/tmp`. SSSD writes the ticket cache under exactly that kind of name:

```ini
# /etc/sssd/sssd.conf (domain section, excerpt)
ldap_id_mapping = True
krb5_ccachedir = /tmp
krb5_ccname_template = FILE:%d/krb5cc_%U
krb5_renewable_lifetime = 7d
krb5_renew_interval = 60m
```

`%U` is the login UID and `%d` is `krb5_ccachedir` (`sssd-krb5(5)`). Renewal keeps a long Jupyter session from losing its ticket.

A container shares the host kernel, so when a container process touches `/workspace`, `rpc.gssd` looks for a ticket cache under that process's UID. The container's UID decides which identity the NAS sees.

### What root gets

The `rpc.gssd` man page is explicit about root: "By default rpc.gssd uses the system's machine credentials for UID 0 accesses that require GSS authentication."

So root in the container reaches the NAS as the machine account, which owns none of the `0700` datasets. Root gets *less* access to the user's files than the user. That is good for security, but any setup step that writes to `/workspace` as root fails.

It failed even earlier for us. The launcher started containers as root with `-w /workspace`. After the share became Kerberos-only, the container no longer started at all: the runtime's `chdir` into `/workspace` ran as root and was denied. The fix was to start the container with `-w /`, so root never touches the NFS path, and to change into `/workspace` only in the exec calls that run as the user.

Our launcher used to create Jupyter directories as root and then `chown -R` them to the user. On a Kerberos mount that step cannot work. We replaced it with a `mkdir` run as the user:

```bash
# Create directories AS THE USER (NFS, owner is the user)
docker exec --user "$UID:$GID" "$name" bash -c '
  mkdir -p /workspace/.jupyter/runtime /workspace/.local/bin /workspace/.cache
'
```

On the host, a playbook running `chmod 0755` on a mounted NFS path failed with `PermissionError: [Errno 1] Operation not permitted`. Permissions belong to the server.

### Running as the user, with root kept inside the container

The launcher now starts every interactive shell and Jupyter process with the host UID and GID:

```bash
docker exec -it --user "$(id -u):$(id -g)" "$name" bash -lc 'cd /workspace && exec bash'
```

Docker accepts a numeric UID for `--user`; only a user *name* must exist in the container. A UID with no entry in the image's `/etc/passwd` still runs, and that is where `I have no name!` comes from. Tools that look up the user, `sudo` among them, break too.

So a separate root step runs first to make the image aware of the user:

```bash
docker exec --user 0 "$name" bash -c "
  grep -q ':x:$GID:' /etc/group  || echo '$USER:x:$GID:' >> /etc/group
  id $UID >/dev/null 2>&1        || echo '$USER:x:$UID:$GID::/workspace:/bin/bash' >> /etc/passwd
  grep -q '^$USER:' /etc/shadow  || echo '$USER:*:19999:0:99999:7:::' >> /etc/shadow
  command -v sudo >/dev/null     || (apt-get update && apt-get install -y sudo)
  echo '$USER ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/lab && chmod 0440 /etc/sudoers.d/lab
"
```

Each line in that block fixed a failure we hit:

- We first used `useradd`. Some images do not have it, so the script now writes to `/etc/passwd` and `/etc/group` directly.
- Images built on the Jupyter Docker Stacks start as `jovyan` (UID 1000). Without `--user 0`, the setup ran as `jovyan` and could not write `/etc/passwd`. The result was `I have no name!` and a broken `sudo`.
- A user that exists in `passwd` but not in `shadow` makes `sudo` fail with `account validation failure`. A non-expiring shadow line with a locked password fixes it.

This `sudo` is container root only: users can `apt install`, but on `/workspace` it is the machine account and cannot read anyone's dataset. Access to the Docker daemon itself is a separate problem, covered in a [separate post on Docker authorization](/posts/docker-authz-opa/).

The `jovyan` case fails in a different way. The host has no ticket cache for UID 1000, so `rpc.gssd` has nothing to present. The user's own UID is the only one that works correctly.

## The UID Mismatch

### Two ID mapping engines, two numbers

AD identifies users by SID. Every Linux system has to turn a SID into a number, and ours used two different engines.

**On the clients, SSSD.** With the AD provider, `ldap_id_mapping` is true by default. The `sssd-ad(5)` man page says the provider maps "UID and GID values from the objectSID parameter in Active Directory", and that "When ID-mapping is enabled, the uidNumber and gidNumber attributes are ignored." The default range runs from 200,000 to 2,000,200,000 in slices of 200,000. Each domain lands in a slice chosen by a hash of its SID, so the resulting UIDs are large and look arbitrary.

**On TrueNAS, winbind with the RID backend.** TrueNAS joins AD through Samba. Its documentation says that by default it "uses an algorithmic method based on the RID component of the user or group SID", and lists the default domain range as 100000001 to 200000000. Our TrueNAS used this default. The Samba man page gives the formula: `ID = RID - BASE_RID + LOW_RANGE_ID`.

Both engines are deterministic, but they start from different bases. For a user with RID 1105, the client computed `1914801105` and TrueNAS computed `100001106`. Neither was wrong, but they were not the same number. We first saw the two numbers side by side when we compared `id` on a client with the owner of the user's files on TrueNAS.

### Where the mismatch hits, layer by layer

The example UIDs are the values before the fix.

| Layer | Where the identity comes from | Example UID | What the mismatch does here |
|---|---|---|---|
| Client host | SSSD ID mapping from SID | 1914801105 | Consistent on the client. `id -u` returns it, and the launcher passes it to the container. If it is ever sent to TrueNAS as a number, it points to the wrong account or to none. |
| Container | `docker exec --user <host UID>` | 1914801105 | Without a `passwd` entry you get `I have no name!` and `sudo` breaks. As root or `jovyan`, NFS requests do not carry the user's identity. |
| NFS request | `rpc.gssd` finds `/tmp/krb5cc_<UID>`, and the principal goes on the wire | `alice@EXAMPLE.LOCAL` | The number is only a lookup key on the client. The mismatch does not matter here, provided the ticket cache is named after the UID the process actually runs as. |
| TrueNAS file system | Principal to UID through winbind idmap, checked against ZFS ownership | 100001106 | The dataset must be owned by *this* number. If ownership is wrong, the user is locked out of their own `0700` dataset. |
| TrueNAS API `filesystem.setperm` | Whatever the provisioning code sends | 100001106 (correct) or 1914801105 (wrong) | Our name-based call did not apply. The dataset stayed `root:root 755`, so the owner could not write and other users could read. Sending the client's UID would have been just as wrong. |
| NFSv4 name mapping (idmapd) | Server sends `EXAMPLE\alice@example.local`, and the client maps it through NSS (SSSD) | client: 1914801105 | If the client's NFSv4 domain does not match, the name does not map and the file shows as `nobody`. If it matches, the name maps to the client's own UID and the mismatch disappears from view. |

### Symptom 1: `nobody:nogroup`

The client logs were full of this:

```text
nfsidmap: libnfsidmap: Unable to determine the NFSv4 domain; Using 'localdomain' as the NFSv4 domain ...
nfsidmap: nss_getpwnam: name 'EXAMPLE\alice@example.local' does not map into domain 'localdomain'
nfsidmap: nss_name_to_gid: name 'EXAMPLE\domain users@example.local' does not map into domain 'localdomain'
```

NFSv4 sends owners as `user@domain` strings, not numbers (`nfsidmap(5)`), and the client accepts only names from its own NFSv4 domain. If `idmapd.conf` does not set one, the client tries a DNS `_nfsv4idmapdomain` record, then the DNS domain. Ours found neither, fell back to `localdomain`, and every owner became `nobody`.

The fix was one line on every client, plus clearing the cache:

```ini
# /etc/idmapd.conf
[General]
Domain = example.local
```

```bash
sudo nfsidmap -c
```

`nfsidmap -c` clears the keyring of cached mappings. The man page warns that a stale key alone can make ownership show as `nobody`.

After that fix, ownership *looked* correct, and the mismatch became invisible: `ls -l` showed `alice`, `ls -ln` showed `1914801105`, and TrueNAS stored `100001106`.

### Symptom 2: the right owner, and still `Permission denied`

At first, the share squashed all access to a single user (`mapall`), which hid ownership problems. Once we removed `mapall` and made the share Kerberos-only, isolation depended on one thing: each dataset's owner had to be exactly the UID that TrueNAS maps the user's principal to.

The first test after that change looked like this:

```text
$ stat -c '%U %u' ~/nfs-home
alice 1914801105
$ touch ~/nfs-home/_t
touch: cannot touch '/home/alice/nfs-home/_t': Permission denied
```

The client resolved the owner to the right user, because idmapd translated the name. The server still refused the write. Three things were wrong at the same time, and each one alone was enough to deny access:

- **The numbers differed.** TrueNAS stored the files under its own UID, which the clients never used.
- **The client did not present the user's ticket.** The cache name had a random suffix (`/tmp/krb5cc_1914801105_XXXXXX`), and `rpc.gssd` did not find it.
- **The server mapped the principal to `nobody`.** Even with the first two fixed, the user still got `ls: cannot open directory ...: Permission denied`. As a diagnostic step, we set the directory to `777` for a moment, and it opened. The user was getting in through the "other" bits, not as the owner. On TrueNAS, `getent passwd 'EXAMPLE\alice'` resolved, but the bare `getent passwd alice` returned nothing.

The fix section covers all three.

### Symptom 3: a new dataset owned by `root:root 755`

The provisioning code set permissions by name:

```python
payload = {
    "path": mount_path,
    "uid": None,
    "user": "EXAMPLE\\alice",
    "group": "EXAMPLE\\domain users",
    "mode": "700",
    "options": {"recursive": True, "traverse": False},
}
```

On our TrueNAS version the call was rejected, and new datasets stayed `root:root 755`. Nobody noticed, because the provisioning code caught the error, logged it as non-critical, and carried on. Older datasets looked fine only because they had been fixed by hand. Under a Kerberos-only share, a `root:root 755` dataset means the owner cannot write to their own home and any other authenticated user can read it. Newer TrueNAS API references list `user` and `group` name fields for `filesystem.setperm`, so name-based calls may behave differently on your version. Numeric IDs avoid the question entirely.

### Why this stays quiet

No error ever mentioned a UID. Every tool showed the right *name*, the symptoms had different names in different layers (`nobody`, `Permission denied` on a file you own, `root:root 755`, `I have no name!`), and Kerberos kept the number off the wire. You only see the mismatch when you put the two numbers side by side.

## The Fix

The fix had three parts. First, make every layer carry the right identity. Second, make the two numbers equal. Third, fix the two lookups that still turned the user into `nobody`.

### Part 1: the right identity at every hop

1. **Names on the wire.** Every client gets `Domain = example.local` in `idmapd.conf` and runs `nfsidmap -c`, so the owner strings from TrueNAS resolve through SSSD to local users.
2. **Principals for access.** The share accepts only `KRB5`, with no `mapall`/`maproot`. The clients mount with `sec=krb5`, run `rpc.gssd`, keep the ticket cache at a predictable `/tmp/krb5cc_%U`, and renew it. Each of these broke at least once after the share became Kerberos-only:
   - The `pam_mount` options did not include `sec=krb5`, so the mount was refused.
   - `rpc-gssd` was not running on every client. It has to be enabled.
   - `rpc.gssd` logged `ERROR: unable to resolve 192.0.2.10 to hostname: Name or service not known`. Without a reverse lookup it cannot build the NFS service principal for the server, and the mount fails. A PTR record in DNS fixes it, or an `/etc/hosts` entry with the server's FQDN.
   - Some users got `ls: cannot access '/home/alice/nfs-home/': Key has expired`. They had logged in while SSSD could not reach the KDC, so they had no valid ticket, and nothing renewed it. The renewal settings shown earlier fix this, and the user has to log out and back in once.
3. **The user's UID inside the container.** Every process that touches `/workspace` runs with `--user <host UID>:<host GID>`. Root work happens in a separate `--user 0` exec that touches only the container's own files.
4. **Ownership from the server's own lookup.** Provisioning asks TrueNAS which UID it uses for the user, then sets ownership numerically:

```python
uid_resp = self._post("/user/get_user_obj", {"username": username})
user_obj = uid_resp.json() or {}
uid, gid = user_obj.get("pw_uid"), user_obj.get("pw_gid")
if not isinstance(uid, int) or not isinstance(gid, int):
    return {"success": False, "message": "could not resolve uid/gid"}

payload = {
    "path": mount_path,
    "mode": "700",
    "uid": uid,
    "gid": gid,
    "options": {"recursive": True, "traverse": False},
}
resp = self._post("/filesystem/setperm", payload)
```

`user.get_user_obj` returns the `struct passwd` fields for a user, including `pw_uid` and `pw_gid`, and it works for Active Directory accounts too. The TrueNAS API documents `uid` and `gid` in `filesystem.setperm` as numeric owner IDs. If TrueNAS cannot resolve the user, provisioning stops instead of leaving a dataset owned by root.

### Part 2: align the UIDs by moving the TrueNAS range

Both engines add the user's RID to a fixed base. TrueNAS uses `range_low - BASE_RID`. SSSD uses the start of the slice the domain landed in. The `sssd-ad(5)` page requires `ldap_idmap_range_size` to be at least the highest RID in the domain, so every user fits in that one slice. If TrueNAS starts its range at the same base, the numbers become equal.

We found the SSSD base by subtracting the RID from the client's UID, for a user and for two groups. All three gave the same number, which confirmed that the client's formula was `base + RID`:

```text
alice          1914801105 - 1105 = 1914800000
domain users   1914800513 -  513 = 1914800000
another group  1914801158 - 1158 = 1914800000
```

Then we changed the TrueNAS side:

1. In the Active Directory idmap settings for the domain, keep the **RID** backend and set **Range Low** to the base and **Range High** to the base plus the SSSD slice size (200,000). The RID backend's SSSD compatibility option stays off; the range is already aligned.
2. TrueNAS would not save idmap changes while the directory service was enabled. Disable the directory service, save, then enable it again.
3. Enabling it makes TrueNAS rejoin the domain. The account used for the join needs write access to the TrueNAS computer object. Without it, the join fails with `Insufficient access` during `LDB_MODIFY`. A `KRB5KDC_ERR_PREAUTH_FAILED` at this step means a wrong password, a wrong UPN format, or a clock difference larger than Kerberos allows.
4. Verify: `wbinfo -i 'EXAMPLE\alice'` on TrueNAS must show the same UID as `id -u alice` on a client.
5. Re-own the existing data, because every AD UID on TrueNAS has changed.

With the example numbers:

```text
client (SSSD):    alice  RID 1105  ->  1914801105
TrueNAS before:   alice  RID 1105  ->  100001106    (range 100000001-200000000)
TrueNAS after:    alice  RID 1105  ->  1914801105   (range 1914800000-1915000000)
```

The re-own step is not optional. ZFS stores numeric owners, so every existing dataset still pointed to the old TrueNAS UIDs. Because the old and the new TrueNAS formulas are both `base + RID`, the shift is the same for every account: `1914800000 - 100000001 = 1814799999`. We took a recursive ZFS snapshot first, then added that shift to every UID and GID in the old range:

```bash
zfs snapshot -r pool/users@before-idmap-fix
OLD_LOW=100000001; OLD_HIGH=200000000; DELTA=1814799999
for U in $(find /mnt/pool/users -printf '%U\n' | sort -un); do
  [ "$U" -ge "$OLD_LOW" ] && [ "$U" -le "$OLD_HIGH" ] &&
    find /mnt/pool/users -uid "$U" -exec chown -h "$((U + DELTA))" {} +
done
# the same loop with %G, -gid and chgrp for groups
```

Two details matter here. Each user's home was its own child dataset, so `find` must not use `-xdev`, or it stops at the first dataset boundary. Local accounts with small UIDs are outside the old range, so the loop leaves them alone.

After the change, the UID was the same for every user we checked on the clients, in the containers, and on TrueNAS. `ls -ln` now shows the same number on both sides. Even when a number does cross the boundary, it identifies the right person.

### Part 3: the two lookups that still returned `nobody`

Aligned numbers were not enough. The user from Symptom 2 still got `Permission denied`, for two more reasons.

**On the client, a ticket cache that `rpc.gssd` could find.** SSSD had written the ticket to `/tmp/krb5cc_1914801105_XXXXXX`, with a random suffix. `rpc.gssd` did not use it. Setting `krb5_ccname_template = FILE:%d/krb5cc_%U` (shown earlier) gave the cache a fixed name. We rolled it out to every client and restarted SSSD. `klist` then showed `/tmp/krb5cc_1914801105`.

**On TrueNAS, "Use Default Domain".** With this setting off, TrueNAS resolved only the domain-qualified name (`EXAMPLE\alice`), and the bare `alice` returned nothing. Incoming principals ended up as `nobody`, which is why the `777` test opened the directory. With the setting on, the bare name resolved to the aligned UID and the `0700` home opened for its owner.

Changing it had its own catch. TrueNAS refused to disable the directory service while it held a Kerberos user credential: `Kerberos user credentials may not be stored for disabled directory services`. We first switched the credential to the TrueNAS machine account's Kerberos principal (its keytab), then disabled, changed the setting, and enabled again. That also removed the password and `PREAUTH_FAILED` problems from later rejoins.

Old mappings are cached on both sides, so after the change we flushed them:

```bash
# TrueNAS
net cache flush
nfsidmap -c
midclt call service.restart nfs
# clients: remount, or log out and back in
```

A few limits apply to this trick:

- It depends on SSSD's slice choice. All clients must use the same SSSD ID mapping settings so they all land in the same slice. The man page warns that hash collisions can make slices differ between machines, so check more than one client.
- Users whose RID is above the SSSD range size do not fit the scheme. The man page says lookups fail for them.
- The TrueNAS docs warn that the domain range "must not overlap with local accounts or trusted domain ranges", and that idmap mistakes can assign permissions to the wrong users. Make the change in a maintenance window and verify before users log in.

## How to Check Your Own Setup

**On the client, find out who you are:**

```bash
id                    # UID and GID as SSSD computed them
getent passwd alice   # the same, from NSS
klist                 # the principal and the ticket cache path; it should end in krb5cc_<your UID>
```

If the cache name does not contain your `id -u`, `rpc.gssd` cannot find it; check `krb5_ccname_template`.

**On TrueNAS, find out who the server thinks you are** (from the TrueNAS shell):

```bash
id 'EXAMPLE\alice'
midclt call user.get_user_obj '{"username": "alice"}'
```

Compare `pw_uid` with the client's `id -u`. If they differ, subtract the RID from the client's UID for two or three accounts. If the result is the same for all of them, that number is the client's base, and the range change above will work.

**Check the NFS mount and the name mapping:**

```bash
findmnt -t nfs4 -o TARGET,SOURCE,OPTIONS   # look for sec=krb5 and vers=4.x
nfsidmap -d                                # effective NFSv4 domain; must not be "localdomain"
grep -E '^\s*Domain' /etc/idmapd.conf
systemctl is-active rpc-gssd
journalctl -t nfsidmap --since today       # "does not map into domain" means wrong domain
```

If the share is Kerberos-only and you still get `Permission denied` on a directory you own, check the server's view of the bare user name (on TrueNAS, `getent passwd alice` without the domain), and check that `klist` shows a cache without a random suffix. To get more detail, stop `rpc-gssd` and run `rpc.gssd -f -vvv` in the foreground while you repeat the access.

**Check ownership both ways:**

```bash
ls -l  ~/nfs-home     # names: should show alice, never nobody
ls -ln ~/nfs-home     # numbers: after alignment, the same UID as on TrueNAS
```

On TrueNAS, `ls -ln /mnt/pool/users/alice` should show the same UID with mode `drwx------`.

**Inside the container:**

```bash
docker exec --user "$(id -u):$(id -g)" mybox sh -c 'id; whoami; ls -ln /workspace | head'
```

`whoami` should print a name, and `/workspace` should show the same UID. Repeat with `--user 0`: listing `/workspace` should fail. If it succeeds, check the share's `maproot` and the dataset's mode.

## Alternatives

**SSSD on POSIX attributes.** Store `uidNumber`/`gidNumber` in AD for every user, set `ldap_id_mapping = False` in SSSD, and select the AD (RFC2307) idmap backend on TrueNAS. Both sides read the same stored number, independent of any slice or range. The costs:

- Every account needs those attributes, assigned uniquely and forever by your provisioning.
- The SSSD man page recommends replicating them to the Global Catalog for performance.
- The man page warns that changing ID mapping options "will cause user and group IDs to change", so every home directory on the clients has to be re-owned.

**Two UID spaces.** Leave the numbers different and rely on Kerberos plus idmapd names, always setting ownership from the server's own lookup. Nothing is migrated, but everyone must know never to send a client UID to the server, which rules out `sec=sys`, NFSv3, and careless numeric backups. We ran like this for a while; it is how the mismatch stayed hidden. (Replacing SSSD with winbind on the clients would also give one algorithm on both sides, at the cost of a new identity stack.)

**The approach we used: move the TrueNAS RID range.** It changes one setting on one server, and it touches neither AD nor the clients. The cost is a one-time re-own of the existing datasets, a rejoin of the NAS to the domain, and a dependency on SSSD's slice. If the SSSD ID mapping settings change, or a client lands in a different slice, the mismatch comes back. For an environment with one NAS and identical clients, that was the cheapest way to get one UID per user everywhere.

## Conclusion

With NFSv4 and Kerberos, identity on the wire is a principal plus a name string, and a numeric UID exists only inside each system. That let our clients and our NAS disagree about every UID with nothing visibly wrong. The problems appeared where a number or a name crossed a boundary: an idmapd domain that defaulted to `localdomain`, a ticket cache that `rpc.gssd` could not find, a server that could not resolve the bare user name, a permission call that failed silently, and containers that ran as root or as the image's built-in user instead of the person at the keyboard.

Three rules came out of this:

- Run container processes that touch Kerberos NFS as the user's host UID, and keep root work inside the container.
- Set ownership on the server with the UID the server itself resolves.
- Compare `id -u` on the client with the server's UID for the same user. If they differ, compute the client's base (UID minus RID), start the server's RID range at that base, and re-own the data.

## References

- `rpc.gssd(8)`, credential cache lookup and machine credentials for UID 0: https://man7.org/linux/man-pages/man8/rpc.gssd.8.html
- `nfsidmap(5)`, NFSv4 `user@domain` names, `-c`, `-d`, stale keys showing as nobody: https://man7.org/linux/man-pages/man5/nfsidmap.5.html
- `idmapd.conf(5)`, `Domain` and its default: https://linux.die.net/man/5/idmapd.conf
- `sssd-ad(5)`, `ldap_id_mapping`, ID ranges and slices, `ldap_idmap_range_size`: https://man.archlinux.org/man/sssd-ad.5.en
- `sssd-krb5(5)`, `krb5_ccname_template`, renewal options: https://man.archlinux.org/man/sssd-krb5.5.en
- Samba `idmap_rid(8)`: https://www.samba.org/samba/docs/current/man-html/idmap_rid.8.html
- TrueNAS, Configuring Active Directory (idmap backends and warnings): https://www.truenas.com/docs/scale/credentials/directoryservices/configad/
- TrueNAS, Directory Services Screens (Range Low/High, default ranges): https://www.truenas.com/docs/scale/credentials/directoryservices/directoryservicesscreens/
- TrueNAS API, `filesystem.setperm`: https://api.truenas.com/v25.10/api_methods_filesystem.setperm.html
- TrueNAS API, `user.get_user_obj`: https://api.truenas.com/v25.10/api_methods_user.get_user_obj.html
- Docker, running containers (default user, `--user`, numeric UIDs): https://docs.docker.com/engine/containers/run/
- Docker CLI, `docker container exec`: https://docs.docker.com/reference/cli/docker/container/exec/
