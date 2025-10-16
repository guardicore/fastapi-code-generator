from pathlib import Path
from typing import Dict, Optional

from datamodel_code_generator.imports import Import, Imports
from datamodel_code_generator.reference import Reference
from datamodel_code_generator.types import DataType

from fastapi_code_generator.parser import OpenAPIParser
from fastapi_code_generator.visitor import Visitor


def _get_most_of_reference(data_type: DataType) -> Optional[Reference]:
    if data_type.reference:
        return data_type.reference
    for data_type in data_type.data_types:
        reference = _get_most_of_reference(data_type)
        if reference:
            return reference
    return None


def get_imports(parser: OpenAPIParser, model_path: Path) -> Dict[str, object]:
    imports = Imports()

    imports.update(parser.imports)
    for data_type in parser.data_types:
        reference = _get_most_of_reference(data_type)
        if reference:
            reference_path = _get_path_of_reference(model_path, reference)
            imports.append(data_type.all_imports)
            imports.append(
                Import.from_full_path(f'.{reference_path}.{reference.name}')
            )
    for from_, imports_ in parser.imports_for_fastapi.items():
        imports[from_].update(imports_)
    for operation in parser.operations.values():
        if operation.imports:
            imports.alias.update(operation.imports.alias)
    return {'imports': imports}

def _get_path_of_reference(model_path, reference):
    m_path = model_path.stem
    try:
        yaml, *_ = reference.path.split('#')
        mod, *_ = yaml.split('.')
        return f"{m_path}.{'.'.join(m for m in mod.split('/'))}"
    except Exception:
        pass
    return m_path


visit: Visitor = get_imports
