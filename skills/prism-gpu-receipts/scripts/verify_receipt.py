#!/usr/bin/env python3
"""Check a public Prism settlement receipt offline, then print what to match on chain.

Usage:
    verify_receipt.py <receipt_id | receipt url | path to receipt.json>
    verify_receipt.py --self-test

Recomputes ``receipt_hash`` from the canonical payload and reconciles the
amounts. Everything here is stdlib: no wallet, no key, no dependency on the
caller having run the lease.

Exit codes, so a caller can branch without parsing the output:

    0   verified and the run completed cleanly
    1   a check failed; this receipt is not evidence
    2   wrong arguments
    3   verified, but the run did not complete cleanly (see ``failure_class``)
"""

import hashlib
import json
import sys
import urllib.request
from collections import OrderedDict

FEED = "https://api.prismnetwork.tech/proof/receipts"
EXPLORER = "https://robinhoodchain.blockscout.com/tx"

OK = 0
FAILED = 1
USAGE = 2
NOT_CLEAN = 3

# Declaration order. Sorting these produces a different digest and a false
# negative; SKILL.md spells out the three canonicalisation rules.
RECEIPT_FIELDS = (
    "receipt_id", "lease_id", "node_id_hash", "gpu_model", "runtime_seconds",
    "charged_base_units", "refunded_base_units", "provider_paid_base_units",
    "failure_class", "outcome", "trust_class", "attestation",
    "credited_seconds", "repro",
)
REPRO_FIELDS = (
    "executor", "token_hash", "spec_hash", "image_digest", "command_hash",
    "result_hash", "stdout_hash", "stderr_hash", "report_hash", "exit_code",
    "expected_exit_code", "succeeded", "truncated",
)
EMPTY_STREAM = hashlib.sha256(b"").hexdigest()

FAILURE_MEANING = {
    "interrupted": "the machine stopped answering before the paid window ended",
    "provisioning_timeout": "the machine never booted",
}


def canonical(receipt):
    payload = OrderedDict()
    for field in RECEIPT_FIELDS:
        # The only optional field that survives as an explicit null. The rest
        # disappear, which is what keeps older receipts byte-identical.
        if field == "failure_class":
            payload[field] = receipt.get("failure_class")
        elif field == "repro" and "repro" in receipt:
            payload[field] = OrderedDict(
                (name, receipt["repro"][name])
                for name in REPRO_FIELDS
                if name in receipt["repro"]
            )
        elif field in receipt:
            payload[field] = receipt[field]
    return json.dumps(payload, separators=(",", ":")).encode()


def load(argument):
    if argument.startswith(("http://", "https://")):
        url = argument
    elif argument.endswith(".json") and "/" in argument:
        with open(argument, encoding="utf-8") as handle:
            return json.load(handle)
    else:
        url = f"{FEED}/{argument}.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def report(receipt):
    """Print the receipt and return an exit code. No I/O beyond stdout."""
    computed = hashlib.sha256(canonical(receipt)).hexdigest()
    published = receipt.get("receipt_hash")
    charged = int(receipt["charged_base_units"])
    refunded = int(receipt["refunded_base_units"])
    paid = int(receipt["provider_paid_base_units"])
    runtime = receipt.get("runtime_seconds")
    # Seconds the lease was HELD AND NOT CHARGED FOR. Reporting it as the
    # metered time understates the run and stops the arithmetic reconciling.
    credited = receipt.get("credited_seconds")
    failure = receipt.get("failure_class")

    failures = []
    if computed != published:
        failures.append(f"receipt_hash mismatch: computed {computed}, published {published}")
    if paid > charged:
        failures.append(f"provider paid {paid} of {charged} charged")
    if receipt.get("outcome") == "disputed":
        failures.append("receipt is disputed and is not final proof")

    print(f"receipt      {receipt['receipt_id']}")
    print(f"escrow       {receipt.get('escrow_address')} lease {receipt.get('chain_lease_id')}")
    print(f"gpu          {receipt.get('gpu_model')}, trust class {receipt.get('trust_class', 'unstated')}")
    print(f"outcome      {receipt.get('outcome')} ({failure or 'ran clean'})")
    print(f"metered      {runtime}s, charged {charged} base units ({charged / 1e6:.6f} USDG)")
    if credited is not None:
        print(f"credited     {credited}s held and NOT charged")
    print(f"deposit      {charged + refunded} base units, {refunded} refunded, {paid} to the provider")
    if "repro" in receipt:
        repro = receipt["repro"]
        print(f"image        {repro.get('image_digest')}")
        print(f"executor     {repro.get('executor')}, exit {repro.get('exit_code')}"
              f" (expected {repro.get('expected_exit_code')})")
        for stream in ("stdout_hash", "stderr_hash"):
            value = repro.get(stream)
            note = " (empty)" if value == EMPTY_STREAM else ""
            print(f"{stream:<12} {value}{note}")
    print(f"receipt_hash {computed} {'ok' if computed == published else 'MISMATCH'}")
    print(f"settlement   {EXPLORER}/{receipt.get('transaction_hash')}")

    if failures:
        print()
        for failure_line in failures:
            print(f"FAIL {failure_line}")
        return FAILED

    print()
    print("Offline checks pass. Still to confirm on Robinhood Chain (id 4663):")
    if receipt.get("outcome") == "refunded":
        # LeaseRefunded's third field is reasonHash, so a refund's receipt hash
        # is self-consistency evidence rather than a value the chain committed.
        print(f"  the escrow at {receipt.get('escrow_address')} emitted one LeaseRefunded")
        print(f"  for leaseId {receipt.get('chain_lease_id')} refunding {refunded},")
        print(f"  carrying the canonical reason hash for {failure}.")
        print("  The receipt hash above is NOT committed by a refund event.")
    else:
        print(f"  the escrow at {receipt.get('escrow_address')} emitted one LeaseFinalized")
        print(f"  for leaseId {receipt.get('chain_lease_id')} with receiptHash 0x{computed},")
        print(f"  charged {charged}, providerPaid {paid}, refunded {refunded},")
        print("  and fee + providerPaid == charged.")

    if failure:
        print()
        print(f"CAUTION this run did not complete cleanly: {failure}"
              f" ({FAILURE_MEANING.get(failure, 'reason not recognised by this checker')}).")
        if credited is not None:
            print(f"        {credited}s of the window were held and never billed.")
        print("        Cite it only with the failure named alongside the claim.")
        return NOT_CLEAN
    return OK


# Two receipts exactly as the feed published them, one clean and one cut short.
# Pinned because the classification below is the part that is easy to get
# backwards: credited_seconds is uncharged time, so a checker that reads it as
# the metered figure reports 152s on a lease that billed 192.
PINNED = {
    "clean": {
        "receipt_id": "25dd3d12-bf11-843b-8770-3b5ba725cc97",
        "lease_id": "10",
        "escrow_address": "0xfd4228eeefc49e4b76a0cd40af9fdd546220b2fd",
        "chain_lease_id": "10",
        "node_id_hash": "0x8126aa45f5829caa2ded18ab38fbe41954f67c5675d70d38278eb6f446077688",
        "gpu_model": "NVIDIA RTX A6000",
        "runtime_seconds": 23,
        "charged_base_units": 5106,
        "refunded_base_units": 394494,
        "provider_paid_base_units": 4596,
        "failure_class": None,
        "outcome": "finalized",
        "trust_class": "open",
        "repro": {
            "executor": "managed",
            "token_hash": "42cac35aedfe5a97a0440e4a47e65d76ddc3c3838ae2f5c945e634ae1a96c8af",
            "spec_hash": "77a4f656283268ae37b735b784eb6a0746cc14451b490eb86a14b60707a3dc75",
            "image_digest": "sha256:2e6d1873c8abd20d50dd311ac76324ef432c0a0396bd71b201b34c633e005930",
            "command_hash": "71a259d3fe338cbf993c5793b332fc8341ad6fa138baf206ed9ff2b486c0e6c3",
            "result_hash": "5cf569bc62fd18dd661f8d49062a2b745016c1c8bf654cc2b7c248ea8d488855",
            "stdout_hash": "736c5b64ce0336f00e2fbf99a8ef7122c9d3f0b29bb6a5ec5a1de673078152a1",
            "stderr_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "report_hash": "ee09088ea9db4bd1d1f47fc11ae8f02b7ce171f21abf63a6e7cc54070b78f82e",
            "exit_code": 0,
            "expected_exit_code": 0,
            "succeeded": True,
            "truncated": False,
        },
        "receipt_hash": "4309cd23f537bd1b3856424743bca35d84ad0b3c67d1ef49de2a258cbde045a5",
        "transaction_hash": "0x200bf5bca1fb873df4ff04fb302ec94680327370a12a11d8d3a696df61960d56",
    },
    "interrupted": {
        "receipt_id": "916ea93f-e7fd-8c59-b20d-80c70019d280",
        "lease_id": "143",
        "escrow_address": "0x62c042265991bea17b07229322a01850974626da",
        "chain_lease_id": "143",
        "node_id_hash": "0x8126aa45f5829caa2ded18ab38fbe41954f67c5675d70d38278eb6f446077688",
        "gpu_model": "RTX 5880Ada",
        "runtime_seconds": 192,
        "charged_base_units": 42624,
        "refunded_base_units": 157176,
        "provider_paid_base_units": 38362,
        "failure_class": "interrupted",
        "outcome": "finalized",
        "trust_class": "open",
        "credited_seconds": 152,
        "receipt_hash": "93517fa85353029e27db4d1c50721bff751e6c8a894b0e50da37574a61e4ecf5",
        "transaction_hash": "0xb8de1c478fab41fb3dc4daf6ff4ac9b8c30d3774282a8c91c5aef176d6cd66b5",
    },
}


def self_test():
    for name, receipt in PINNED.items():
        computed = hashlib.sha256(canonical(receipt)).hexdigest()
        assert computed == receipt["receipt_hash"], (
            f"{name}: canonicalisation disagrees with the published hash"
        )
        print(f"--- {name} ---")
        code = report(receipt)
        expected = OK if name == "clean" else NOT_CLEAN
        assert code == expected, f"{name}: exit {code}, expected {expected}"
        print()

    interrupted = PINNED["interrupted"]
    rate = interrupted["charged_base_units"] / interrupted["runtime_seconds"]
    assert rate == 222, (
        "the charge divides by runtime_seconds at the quoted rate, so a checker "
        "that meters credited_seconds is reporting the wrong number"
    )

    tampered = dict(PINNED["clean"], charged_base_units=1)
    assert report(tampered) == FAILED, "an edited receipt has to fail"

    print()
    print(f"{len(PINNED)} pinned receipt(s) verified, tampering rejected")
    return OK


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        return self_test()
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return USAGE
    return report(load(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
