from wps._vector_geometry import binary_process, execute_binary_predicate


PROCESS = binary_process(
    "vector.disjoint", "벡터 비접촉 선택 (Disjoint)", "Vector disjoint", "disjoint"
)


def execute(layer, parameters, context):
    return execute_binary_predicate(layer, parameters, context, "disjoint")
