def cleaned_json_schema(cls):
    """

    Generates a JSON schema for the given class and its fields, but eliminates
    all fields which are models, embedded models, or references to models.

    :param cls:
    :return:
    """
    schema = cls.model_json_schema()
    schema["title"] = cls.__name__

    # for field_name, field in cls.model_fields.items():
    #     field_type = field.annotation
    #     if hasattr(field_type, '_is_simstack_model'):
    #         # If the field is a model or embedded model, remove its proprties
    #         if field_name in schema['$defs'] and "properties" in schema['$defs']['properties']:
    #             del schema['$defs']['properties']

    return schema
