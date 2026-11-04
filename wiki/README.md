# Wiki sources

The pages of the [PyICs wiki](https://github.com/johannes-langer/PyICs/wiki), a guide for instructors. Edit them here, not in the wiki's web editor: the [Wiki workflow](../.github/workflows/wiki.yml) publishes them on every push to `main` that changes them.

- Each `Page-Name.md` becomes the wiki page `Page-Name`. `_Sidebar.md` and `_Footer.md` are shown on every page. This README isn't published.
- The test references are **not** copied here: [`simple_test.md`](../simple_test.md), [`structure_test.md`](../structure_test.md) and [`general_test.md`](../general_test.md) are published as `SimpleTest-Reference`, `StructureTest-Reference` and `GeneralTest-Reference`.
- Link to pages wiki-style, without `.md`: `[Creating Tasks](Creating-Tasks#editing-a-task)`. Links between pages and to headings are checked, and a broken one fails the workflow, also on pull requests.
- Pages deleted here aren't deleted from the wiki. Delete them in the wiki, too.

Build and check the pages locally:

```sh
python3 .github/scripts/build_wiki.py /tmp/pyics-wiki
```

## First-time setup

The workflow can only push to a wiki that exists. Once, before the first run:

1. In the repository's **Settings > General > Features**, enable **Wikis**.
2. Open the **Wiki** tab and save a first page (any content; it is replaced by `Home.md`).
3. Run the **Wiki** workflow from the **Actions** tab (or push a change to `wiki/`).
