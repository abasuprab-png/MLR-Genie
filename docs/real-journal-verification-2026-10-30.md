# Real-journal verification — October 30, 2026

A requester token successfully verified a reworded claim against an actual
publisher PDF with unmocked Gemini. This used an isolated loopback server in
hosted security mode, temporary SQLite/local artifacts, and temporary credentials.
It did not deploy a service or establish Cloud SQL, GCS, Vertex, Azure, or public
HTTPS connectivity.

The source was John P. A. Ioannidis, *Why Most Published Research Findings Are
False*, PLOS Medicine 2(8):e124 (2005), DOI `10.1371/journal.pmed.0020124`.
[PubMed citation](https://pubmed.ncbi.nlm.nih.gov/16060722/) and
[publisher PDF](https://journals.plos.org/plosmedicine/article/file?id=10.1371/journal.pmed.0020124&type=printable).
This open-access methods article is a software verification fixture, not a
clinical treatment recommendation. The six-page PDF contains 255629 bytes and
has SHA-256 `ffc1005680cb620eec4c913437dfabbf311b535cfe16cbaeb2faec1f92afc362`.
The PDF is not copied into the repository.

Reworded test claim:

> Within the paper’s framework, research findings are less likely to be true in
> fields where studies are smaller.

The endpoint returned `supported`, with an anchor on page 2. A separate PyMuPDF
check confirmed the rectangle stayed within that page, the extracted rectangle
text matched the returned quote, and the page dimensions matched the uploaded
PDF. Health returned 200 and tokenless CAMS returned 401. The run made two live
Gemini calls and returned one live claim receipt. No credentials or bearer tokens
are retained in the [sanitized evidence](real-journal-verification-2026-10-30.json);
its backend and evaluator source hashes match the checked files.

`tests/test_live_journal_verification.py` passed 11 focused checks, including
independent quote/rectangle checks, invalid and nonfinite coordinates, wrong
pages/dimensions/verdicts, and unsafe remote token destinations. The evaluator
rejects remote operation without the deployed bootstrap secret and never
generates credentials for an existing service.

For the eventual production check, securely supply the deployed bootstrap secret
and run:

```sh
.venv/bin/python evals/live_journal_verification.py \
  --base-url 'https://<public-host>' \
  --reference-pdf '/path/to/ioannidis-2005.pdf' \
  --claim 'Within the paper’s framework, research findings are less likely to be true in fields where studies are smaller.' \
  --source-url 'https://pubmed.ncbi.nlm.nih.gov/16060722/'
```

Omit `--base-url` to repeat the isolated local hosted check. The source URL is
attribution metadata; the evaluator does not fetch it. Review the actual uploaded
PDF and its hash. The remote report distinguishes `external-https` from
`isolated-loopback`; record it alongside the public hostname and registry image
digest after deployment.

The current environment still has no GCP deployment identity or selected project
and controlled hostname. Google Cloud SDK 591.0.0 was installed for the deployment
step, but no login, cloud resource creation, image push, or Terraform apply was
performed. Authorization to deploy is already provided; the remaining setup is
account access and the deployment target.
