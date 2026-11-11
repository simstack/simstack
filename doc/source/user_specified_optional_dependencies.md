<!-- '''this is a README for the user-specified "extras"/optional dependencies''' -->

# User specified dependencies

Usually optional dependencies are used in projects that have e.g. a gui and a non-gui version, so that the installation from
`pyproject.toml`only requires the ones that not everyone needs and then an installation with e.g. `uv sync --locked --extra NAME_OF_OPTIONAL_DEPENDENCY`if using uv or similar for other systems.

## Adding optinal dependencies

Dependencies should only be added now as an INTERIM solution or a general dependency that is of use to all project users.
After adding it to `pyproject.toml`, record resource-specific defaults in
`user_extra_config.toml` at the workflow repository root and include it in the
published revision. Server also accepts per-resource extras overrides when
publishing a repository revision.

Server records the selected extras with the pinned repository revision. Runner
prepares that revision using `uv sync --locked --extra NAME_OF_OPTIONAL_DEPENDENCY`
through the API control flow. Different revisions and extras profiles use separate
runtimes; preparing a new runtime preserves environments used by running tasks.


## Content of the config file

you need a valid toml file containing the section `[optional_dependencies_desired_by_ressource]`
This then contains key value pairs where the key corresponds to the string of the "Resource-class" value and a list over the extra.
e.g. 

`int-nano=["rmsd_analysis"]`

## For devs

If you plan to modify this - take care of the following things: 

    - the tomllib makes nested dicts.  
    - the `Resource`class has some overrides. 


## TLDR

add `user_extra_config.toml` to the workflow repository before publishing its revision.
It should contain something like 
```
[optional_dependencies_desired_by_ressource]
int-nano=["rmsd_analysis"]
```
and contain a keywoard specified as optional dependencies in the `pyproject.toml.` 
