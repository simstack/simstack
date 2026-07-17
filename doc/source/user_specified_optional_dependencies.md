<!-- '''this is a README for the user-specified "extras"/optional dependencies''' -->

# User specified dependencies

Usually optional dependencies are used in projects that have e.g. a gui and a non-gui version, so that the installation from
`pyproject.toml`only requires the ones that not everyone needs and then an installation with e.g. `uv sync --locked --extra NAME_OF_OPTIONAL_DEPENDENCY`if using uv or similar for other systems.

## Adding optinal dependencies

Dependencies should only be added now as an INTERIM solution or a general dependency that is of use to all project users.
After adding it to the  `pyproject.toml`, add a `user_extra_config.toml` to your runner folder. This one specifies for YOUR USER ONLY and the 
corresponding RESOURCE only the extra to use. 
This should not go into `config.toml`or similar. `config.toml`is reserved for multi-user project-resource specifications, whereas the `user_extra_config.toml` 
is your personal config file.

Intial installation sould work normally `uv sync --locked --extra NAME_OF_OPTIONAL_DEPENDENCY` this has been added to the simstack-core package (`services/git_uv_update_service.py`) - it has been tested locally but does not yet have its own unit-test. Use the function at your own risk. Testing is simple: Just run python3 manually from your `.venv` and check if the desired packages are there. The logger should also catch cases where the resource are wrongly specified. 


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

add `user_extra_config.toml` to your runner.
It should contain something like 
```
[optional_dependencies_desired_by_ressource]
int-nano=["rmsd_analysis"]
```
and contain a keywoard specified as optional dependencies in the `pyproject.toml.` 
