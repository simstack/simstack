from odmantic import Model


class InputTemplate(Model):
    model_mapping: str
    name: str
    input: str
    parameters: str
