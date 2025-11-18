External and Internal Files and Resources
=========================================

Workflows need access to external resources, such as files, databases, and URLs.
External resources are only input and otherwise not managed. Internal resources, mostly
files, are managed by the workflow by certain specfications.

External resources differ from internal resources in that we cannot decide based
on the data describing the resource, whether its "value" or content has changed
since the last call.


Input Data
~~~~~~~~~~

Files can (mostly) be hashed. For all other use cases an intermediate storage (electron) can be implemented that
stores the "query" and the "result" of the query (assuming that the query and the result can be hashed).
This service electron returns the result with a flag whether the result has changed.


.. code-block:: python

    def complex_compute(data, reuse=True):
        # complex computation based on data
        return result

    def compute_electron(url):
        result, changed = get_external(url)
        if changed:
            result = complex_compute(url)
            electron.set(hash, result)
        return result

    def get_external(query):
        hash = hash(query)
        result, changed = get(hash)
        if changed:
            result = query_data(query)
            electron.set(hash, result)
        return result


## Output Data

Only Output Data is generated in SimStack II. Because "human readable" data is core concept of the new version all
digestible output should be parsed to JSON. The question is how to handle large-scale data, which is costly to
generate and which may have un-anticipated uses later on. An example would be MD trajectories.

This data should be handled as part of the research data management plan, which we plan to implement as an integral
part of the WF environment anyway. Every WANO will upload all inputs and outputs to the RDM storage anyway.
The JSON output should thus contain records of all files that have been uploaded.

The issue arising with large data is whether it makes sense to pass only the remote info
When a WANO is re-executed