from wps._vector_geometry import binary_process, execute_binary_predicate


PROCESS = binary_process(
    "vector.contains", "벡터 포함 선택 (Contains)", "Vector contains", "contains"
)


def execute(layer, parameters, context):
    return execute_binary_predicate(layer, parameters, context, "contains")
