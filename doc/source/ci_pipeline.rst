.. _ci_pipeline:

CI Pipeline and Documentation Artifacts
=======================================

The SimStack II project uses GitHub Actions for Continuous Integration. One of the jobs in the pipeline is `build-docs`, which automatically generates the API documentation and builds the Sphinx HTML documentation.

Where to find the documentation artifact on GitHub
--------------------------------------------------

When a CI run completes, you can find the built documentation as follows:

1. Go to the **Actions** tab in the GitHub repository.
2. Click on the most recent workflow run (usually named "CI").
3. Scroll down to the **Artifacts** section at the bottom of the summary page.
4. You will see an artifact named `sphinx-docs`. You can download it as a ZIP file.

How to use the artifact in another repository
---------------------------------------------

If you want to use the documentation artifact in another repository's GitHub Action (for example, to deploy it to a web server), you can use the `actions/download-artifact` action.

Since artifacts are associated with a specific workflow run, you typically need to know the run ID. However, for the most recent successful run, you can use the `dawidd6/action-download-artifact` action or the GitHub CLI.

Example workflow for another repository:
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Below is an example of how to download the `sphinx-docs` artifact from this repository in another GitHub Action:

.. code-block:: yaml

    name: Update Server Documentation
    on:
      workflow_run:
        workflows: ["CI"]
        types: [completed]
        branches: [main]

    jobs:
      deploy:
        runs-on: ubuntu-latest
        if: ${{ github.event.workflow_run.conclusion == 'success' }}
        steps:
          - name: Download sphinx-docs artifact
            uses: dawidd6/action-download-artifact@v6
            with:
              github_token: ${{ secrets.GITHUB_TOKEN }}
              workflow: ci.yml
              name: sphinx-docs
              repo: your-org/simstack  # Replace with the actual repository path
              run_id: ${{ github.event.workflow_run.id }}

          - name: Deploy to Server
            run: |
              # Your deployment commands here
              # The documentation is now in the current directory (or the path you specified)
              ls -R

Note: You may need a Personal Access Token (PAT) if the repository is private or if you are accessing it across different organizations.
