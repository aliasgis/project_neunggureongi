from wps._vector_geometry import binary_process, execute_binary_predicate


PROCESS = binary_process(
    "vector.within", "벡터 내부 선택 (Within)", "Vector within", "within"
)


def execute(layer, parameters, context):
    return execute_binary_predicate(layer, parameters, context, "within")
