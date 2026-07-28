import re
import sys
import json
from datetime import datetime, timezone
from functools import lru_cache
from collections import defaultdict
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import typer
from click import Abort, ClickException, Command
from datamodel_code_generator import LiteralType, chdir
from datamodel_code_generator.enums import DataModelType
from datamodel_code_generator.format import CodeFormatter, PythonVersion, DatetimeClassType
from datamodel_code_generator.model import get_data_model_types
from jinja2 import Environment, FileSystemLoader, select_autoescape
from typer.main import get_command

from fastapi_code_generator.parser import OpenAPIParser
from fastapi_code_generator.version import __version__
from fastapi_code_generator.visitor import Visitor

app = typer.Typer()

all_tags: List[str] = []

TITLE_PATTERN = re.compile(r'(?<!^)(?<![A-Z -])(?=[A-Z])|[ -]+')

BUILTIN_MODULAR_TEMPLATE_DIR = Path(__file__).parent / "modular_template"

BUILTIN_TEMPLATE_DIR = Path(__file__).parent / "template"

BUILTIN_VISITOR_DIR = Path(__file__).parent / "visitors"

MODEL_PATH: Path = Path("models")


@lru_cache(maxsize=None)
def _get_code_formatter(
    python_version: PythonVersion, settings_path: Path
) -> CodeFormatter:
    return CodeFormatter(python_version, settings_path)


@lru_cache(maxsize=None)
def _get_template_environment(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(template_dir, encoding="utf8"),
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml"),
            default_for_string=False,
        ),
    )


def dynamic_load_module(module_path: Path) -> Any:
    module_name = module_path.stem
    spec = spec_from_file_location(module_name, str(module_path))
    if spec:
        module = module_from_spec(spec)
        if spec.loader:
            spec.loader.exec_module(module)
            return module
    raise Exception(f"{module_name} can not be loaded")  # pragma: no cover


def _normalize_pydantic_v2_code(code: str) -> str:
    return code.replace("constr(regex=", "constr(pattern=")


def _show_version(value: bool) -> None:
    if value:
        print(f"fastapi-codegen {__version__}")
        raise typer.Exit()


def _resolve_remote_reference_options(
    allow_remote_refs: Optional[bool], allow_private_network: bool
) -> tuple[Optional[bool], bool]:
    match allow_remote_refs, allow_private_network:
        case False, True:
            return False, False
        case None, True:
            return True, True
        case _:
            pass
    return allow_remote_refs, allow_private_network


def _parse_specified_tags(specify_tags: Optional[str]) -> set[str]:
    if not specify_tags:
        return set()
    return {tag for raw_tag in specify_tags.split(",") if (tag := raw_tag.strip())}


@lru_cache(maxsize=1)
def _get_command() -> Command:
    return get_command(app)


@app.command()
def main(
    encoding: str = typer.Option("utf-8", "--encoding", "-e"),
    input_file: str = typer.Option(..., "--input", "-i"),
    output_dir: Path = typer.Option(..., "--output", "-o"),
    model_file: str = typer.Option(None, "--model-file", "-m"),
    template_dir: Optional[Path] = typer.Option(None, "--template-dir", "-t"),
    model_template_dir: Optional[Path] = typer.Option(None, "--model-template-dir"),
    enum_field_as_literal: Optional[LiteralType] = typer.Option(
        None, "--enum-field-as-literal"
    ),
    generate_routers: bool = typer.Option(False, "--generate-routers", "-r"),
    specify_tags: Optional[str] = typer.Option(None, "--specify-tags"),
    custom_visitors: Optional[List[Path]] = typer.Option(
        None, "--custom-visitor", "-c"
    ),
    disable_timestamp: bool = typer.Option(False, "--disable-timestamp"),
    strict_nullable: bool = typer.Option(
        False,
        "--strict-nullable",
        help="Respect explicit OpenAPI nullable flags when generating models.",
    ),
    include_request_argument: bool = typer.Option(
        False,
        "--include-request-argument",
        help=(
            "Auto-inject a FastAPI Request parameter into operations when not "
            "present."
        ),
    ),
    allow_remote_refs: Optional[bool] = typer.Option(
        None,
        "--allow-remote-refs/--no-allow-remote-refs",
        help=(
            "Allow or block fetching remote $ref targets over HTTP/HTTPS. "
            "The default follows datamodel-code-generator compatibility behavior."
        ),
    ),
    allow_private_network: bool = typer.Option(
        False,
        "--allow-private-network",
        help=(
            "Allow trusted remote $ref targets on local or private network "
            "addresses."
        ),
    ),
    output_model_type: DataModelType = typer.Option(
        DataModelType.PydanticV2BaseModel.value, "--output-model-type", "-d"
    ),
    python_version: PythonVersion = typer.Option(
        PythonVersion.PY_310.value, "--python-version", "-p"
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_show_version,
        is_eager=True,
    ),
    use_annotated: bool = typer.Option(
        False,
        "--use-annotated",
        help="Use typing.Annotated for generated model field constraints.",
    ),
    reuse_model: bool = typer.Option(
        False,
        "--reuse-model",
        help="Reuse identical generated models as the same type.",
    ),
    enable_faux_immutability: bool = typer.Option(
        False,
        "--enable-faux-immutability",
        help=(
            "Generate frozen Pydantic models so instances are hashable when their "
            "fields are hashable."
        ),
    ),
    capitalise_enum_members: bool = typer.Option(False, "--capitalise-enum-members"),
    output_datetime_class: Optional[DatetimeClassType] = typer.Option(
        DatetimeClassType.Datetime, "--output-datetime-class",
        help="Specify the datetime class to use for datetime fields"
    ),
    allow_population_by_field_name: bool = typer.Option(False, "--allow-population-by-field-name"),
    extra_template_data: str = typer.Option(None, "--extra-template-data"),
    additional_imports: str = typer.Option(None, "--additional-imports"),
) -> None:
    del version
    input_name = Path(input_file).expanduser().resolve()
    input_text: Optional[str] = None

    try:
        with open(input_file, encoding=encoding) as f:
            input_text = f.read()
    except:
        pass

    if extra_template_data:
        try:
            with open(extra_template_data, encoding=encoding) as f:
                extra_template_data = json.load(f, object_hook=lambda d: defaultdict(dict, **d))
        except Exception as exc:
            print(f"could not load extra: {exc}")

    model_path = Path(model_file) if model_file else MODEL_PATH  # pragma: no cover

    if additional_imports:
        additional_imports = additional_imports.split(",")

    return generate_code(
        input_name,
        input_text,
        encoding,
        output_dir,
        template_dir,
        model_template_dir,
        model_path,
        enum_field_as_literal=enum_field_as_literal or None,
        custom_visitors=custom_visitors,
        disable_timestamp=disable_timestamp,
        strict_nullable=strict_nullable,
        include_request_argument=include_request_argument,
        allow_remote_refs=allow_remote_refs,
        allow_private_network=allow_private_network,
        generate_routers=generate_routers,
        specify_tags=specify_tags,
        output_model_type=output_model_type,
        python_version=python_version,
        use_annotated=use_annotated,
        reuse_model=reuse_model,
        enable_faux_immutability=enable_faux_immutability,
        capitalise_enum_members=capitalise_enum_members,
        output_datetime_class=output_datetime_class,
        allow_population_by_field_name=allow_population_by_field_name,
        extra_template_data=extra_template_data,
        additional_imports=additional_imports
    )


def invoke_main(args: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)

    try:
        result = _get_command().main(
            args=argv,
            prog_name="fastapi-codegen",
            standalone_mode=False,
        )
    except ClickException as exc:
        exc.show()
        return exc.exit_code
    except Abort:  # pragma: no cover
        return 1

    return int(result) if isinstance(result, int) else 0


def generate_code(
    input_name: str,
    input_text: str,
    encoding: str,
    output_dir: Path,
    template_dir: Optional[Path],
    model_template_dir: Optional[Path] = None,
    model_path: Optional[Path] = None,
    enum_field_as_literal: Optional[LiteralType] = None,
    custom_visitors: Optional[List[Path]] = None,
    disable_timestamp: bool = False,
    strict_nullable: bool = False,
    include_request_argument: bool = False,
    allow_remote_refs: Optional[bool] = None,
    allow_private_network: bool = False,
    generate_routers: Optional[bool] = None,
    specify_tags: Optional[str] = None,
    output_model_type: DataModelType = DataModelType.PydanticV2BaseModel,
    python_version: PythonVersion = PythonVersion.PY_310,
    use_annotated: bool = False,
    reuse_model: bool = False,
    enable_faux_immutability: bool = False,
    capitalise_enum_members: bool = False,
    output_datetime_class: Optional[DatetimeClassType] = None,
    extra_template_data: defaultdict[str, dict[str, Any]] | None = None,
    allow_population_by_field_name: Optional[bool] = False,
    additional_imports: Optional[list[str]] = None
) -> None:
    global all_tags
    if not model_path:  # pragma: no cover
        model_path = MODEL_PATH
    if not output_dir.exists():
        output_dir.mkdir(parents=True)
    if generate_routers:
        Path(output_dir / "routers").mkdir(parents=True, exist_ok=True)
    if not template_dir:
        template_dir = (
            BUILTIN_MODULAR_TEMPLATE_DIR if generate_routers else BUILTIN_TEMPLATE_DIR
        )
    if not custom_visitors:
        custom_visitors = []
    data_model_types = get_data_model_types(output_model_type, python_version)
    code_formatter = _get_code_formatter(python_version, Path().resolve())
    allow_remote_refs, allow_private_network = _resolve_remote_reference_options(
        allow_remote_refs, allow_private_network
    )
    source = input_text or input_name
    parser = OpenAPIParser(
        source=source,
        enum_field_as_literal=enum_field_as_literal,
        data_model_type=data_model_types.data_model,
        data_model_root_type=data_model_types.root_model,
        data_model_field_type=data_model_types.field_model,
        data_type_manager_type=data_model_types.data_type_manager,
        dump_resolve_reference_action=data_model_types.dump_resolve_reference_action,
        custom_template_dir=model_template_dir,
        target_python_version=python_version,
        strict_nullable=strict_nullable,
        include_request_argument=include_request_argument,
        allow_remote_refs=allow_remote_refs,
        allow_private_network=allow_private_network,
        use_annotated=use_annotated,
        reuse_model=reuse_model,
        enable_faux_immutability=enable_faux_immutability,
        additional_imports=additional_imports,
        base_path=Path(input_name).absolute().parent,
        capitalise_enum_members=capitalise_enum_members,
        output_datetime_class=output_datetime_class,
        extra_template_data=extra_template_data,
        allow_population_by_field_name=allow_population_by_field_name,
        field_extra_keys={"union_mode"}
    )

    with chdir(output_dir):
        models = parser.parse(format_=False)
    if not models:
        # if no models (schemas), just generate an empty model file.
        modules = {output_dir / model_path.with_suffix('.py'): ("", input_name)}
    elif isinstance(models, str):
        output_path = output_dir / model_path.with_suffix('.py')
        modules = {
            output_path: (
                code_formatter.format_code(models),
                input_name,
            )
        }
    else:
        modules = {
            output_dir
            / model_path
            / Path(*module_name): (
                code_formatter.format_code(model.body),
                input_name,
            )
            for module_name, model in models.items()
        }

    environment = _get_template_environment(template_dir.resolve())

    results: Dict[Path, str] = {}

    template_vars: Dict[str, object] = {"info": parser.parse_info()}
    visitors: List[Visitor] = []
    all_tags = []

    # Load visitors
    builtin_visitors = BUILTIN_VISITOR_DIR.rglob("*.py")
    visitors_path = [*builtin_visitors, *(custom_visitors if custom_visitors else [])]
    for visitor_path in visitors_path:
        module = dynamic_load_module(visitor_path)
        if hasattr(module, "visit"):
            visitors.append(module.visit)
        else:
            raise Exception(f"{visitor_path.stem} does not have any visit function")

    # Call visitors to build template_vars
    for visitor in visitors:
        visitor_result = visitor(parser, model_path)
        template_vars = {**template_vars, **visitor_result}

    if generate_routers:
        operations: Any = template_vars.get("operations", [])
        for operation in operations:
            if hasattr(operation, "tags"):
                for tag in operation.tags:
                    all_tags.append(tag)
    # Convert from Tag Names to router_names
    sorted_tags = sorted(set(all_tags), key=lambda x: x.lower())
    routers = [re.sub(TITLE_PATTERN, '_', tag.strip()).lower() for tag in sorted_tags]
    router_tag_pairs = list(zip(routers, sorted_tags, strict=True))
    specified_tags = set()
    existing_main_has_router_includes = False
    if generate_routers and specify_tags:
        specified_tags = _parse_specified_tags(specify_tags)
        main_path = output_dir / "main.py"
        if main_path.exists():
            existing_main_has_router_includes = (
                "app.include_router" in main_path.read_text(encoding=encoding)
            )

    main_router_tag_pairs = router_tag_pairs
    if specified_tags and not existing_main_has_router_includes:
        main_router_tag_pairs = [
            (router, tag) for router, tag in router_tag_pairs if tag in specified_tags
        ]
        if not main_router_tag_pairs:
            available = ", ".join(tag for _, tag in router_tag_pairs) or "<none>"
            requested = ", ".join(sorted(specified_tags))
            raise ClickException(
                f"No routers matched --specify-tags ({requested}). "
                f"Available tags: {available}"
            )

    template_vars = {
        **template_vars,
        "routers": [router for router, _ in main_router_tag_pairs],
        "tags": [tag for _, tag in main_router_tag_pairs],
    }

    for target in template_dir.rglob("*"):
        relative_path = target.relative_to(template_dir)
        if generate_routers and relative_path.name.startswith("routers."):
            continue
        template = environment.get_template(str(relative_path))
        result = template.render(template_vars)
        results[relative_path] = _normalize_pydantic_v2_code(
            code_formatter.format_code(result)
        )

    if generate_routers:
        results.pop(Path("routers.jinja2"), None)
        router_pairs = router_tag_pairs
        if specified_tags and not existing_main_has_router_includes:
            router_pairs = main_router_tag_pairs

        for target in template_dir.rglob("routers.*"):
            relative_path = target.relative_to(template_dir)
            for router, tag in router_pairs:
                if (
                    not Path(output_dir.joinpath("routers", router))
                    .with_suffix(".py")
                    .exists()
                    or not specified_tags
                    or tag in specified_tags
                ):
                    template_vars["tag"] = tag.strip()
                    template = environment.get_template(str(relative_path))
                    result = template.render(template_vars)
                    router_path = Path("routers", router).with_suffix(".jinja2")
                    results[router_path] = _normalize_pydantic_v2_code(
                        code_formatter.format_code(result)
                    )

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    header = f"""\
# generated by fastapi-codegen:
#   filename:  {Path(input_name).name}"""
    if not disable_timestamp:
        header += f"\n#   timestamp: {timestamp}"

    for path, code in results.items():
        with output_dir.joinpath(path.with_suffix(".py")).open(
            "wt", encoding=encoding
        ) as file:
            print(header, file=file)
            print("", file=file)
            print(code.rstrip(), file=file)

    header = """\
# generated by fastapi-codegen:
#   filename:  {filename}"""
    if not disable_timestamp:
        header += f'\n#   timestamp: {timestamp}'

    for path, body_and_filename in modules.items():
        body, filename = body_and_filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wt", encoding="utf8") as file:
            print(header.format(filename=filename), file=file)
            if body:
                print("", file=file)
                print(_normalize_pydantic_v2_code(body).rstrip(), file=file)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(invoke_main())
