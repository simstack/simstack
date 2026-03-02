Developing new Simstack Code
============================

Because the server-db-runner architecture is quite complex here
are some best-practices so far to develop new simstack modules


General Hints
~~~~~~~~~~~~~~

- Whenever you make a new Model, run create_model_table again. This is
  incremental, i.e. if you have a lot of code, its enough to run it
  on the specific directory. You have to rerun this every time you change
  the structure of the Model.
- Models visible in the UI should be decorated with `@simstack_model`
- Whenever you create a new node run create_node_table
- The default runner will run both create_node_table and create_model_table
  after a git pull. For this you have to set the `active_dirs` variable in config.toml
  in your project root
- Develop models for the terminal nodes first. If you change their structure you
  will invalidate all existing runs (see: tips for migration)

-
