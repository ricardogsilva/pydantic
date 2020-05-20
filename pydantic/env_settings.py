import collections.abc
import os
import warnings
from pathlib import Path
from typing import AbstractSet, Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from .fields import ModelField
from .main import BaseModel, Extra
from .typing import display_as_type
from .utils import deep_update, sequence_like


class SettingsError(ValueError):
    pass


def read_additional_values(
    getter: Callable[..., Dict[str, str]], is_case_sensitive: bool, env_prefix: str, **getter_kwargs: Any
) -> Dict[str, str]:
    additional_values = getter(**getter_kwargs)
    return normalize_items(additional_values, is_case_sensitive, env_prefix)


def read_env_file(file_path: Path) -> Dict[str, Optional[str]]:
    try:
        from dotenv import dotenv_values
    except ImportError as e:
        raise ImportError('python-dotenv is not installed, run `pip install pydantic[dotenv]`') from e

    file_vars: Dict[str, Optional[str]] = dotenv_values(file_path)
    return file_vars


def read_env_file_from_named_env_var(env_var: str) -> Dict[str, str]:
    path = Path(os.getenv(env_var))
    return read_env_file(path)


def read_filesystem_directory(directory: Path) -> Dict[str, str]:
    """Read values from a secrets directory"""
    result = {}
    if directory.is_dir():
        for item in directory.iterdir():
            if item.is_file():
                contents = item.read_text(encoding='utf-8').strip()
                result[item.name] = contents
    return result


# TODO: remove this
def normalize_items(items: Dict[str, str], case_sensitive: bool, env_prefix: str) -> Dict[str, str]:
    print(f'locals: {locals()}')
    result = {}
    for name, value in items.items():
        if value is None:
            continue
        elif case_sensitive:
            new_name = name
        else:
            new_name = name.lower()
        new_name = new_name.replace(env_prefix, '')
        result[new_name] = value
    return result


class BaseSettings(BaseModel):
    """
    Base class for settings, allowing values to be overridden by environment variables.

    This is useful in production for secrets you do not wish to save in code, it plays nicely with docker(-compose),
    Heroku and any 12 factor app design.
    """

    def __init__(__pydantic_self__, **values: Any) -> None:
        # Uses something other than `self` the first arg to allow "self" as a settable attribute
        additional_values = __pydantic_self__._get_additional_values()
        environ_values = __pydantic_self__._build_environ()
        consolidated = __pydantic_self__._build_values(values, environ_values, additional_values)
        super().__init__(**consolidated)

    def _build_values(
        self, init_kwargs: Any, environ_values: Dict[str, Optional[str]], additional_values: Dict[str, str]
    ) -> Dict[str, str]:
        # Precedence rules (first means highest priority):
        # 1. Arguments to `__init__`
        # 2. Environment variables
        # 3. Values gotten from additional methods (.env files, etc)
        # 4. Defaults in the class definition
        return deep_update(deep_update(additional_values, environ_values), init_kwargs)

    def _get_additional_values(self) -> Dict[str, str]:
        additional_getters: List[
            Union[Callable[..., Dict[str, str]], Tuple[Callable[..., Dict[str, str]], Dict[str, Any]]]
        ] = self.__config__.additional_getters or []
        result = {}
        for getter_info in additional_getters:
            if isinstance(getter_info, collections.abc.Callable):
                getter: Callable[..., Dict[str, str]] = getter_info
                getter_kwargs = {}
            else:
                getter: Callable[..., Dict[str, str]] = getter_info[0]
                getter_kwargs = getter_info[1]
            getter_values = getter(**getter_kwargs)
            normalized = self._normalize_items(getter_values)
            result.update(normalized)
        return result

    def _build_environ(self) -> Dict[str, Optional[str]]:
        """
        Build environment variables suitable for passing to the Model.
        """
        d: Dict[str, Optional[str]] = {}

        if self.__config__.case_sensitive:
            env_vars: Mapping[str, Optional[str]] = os.environ
        else:
            env_vars = {k.lower(): v for k, v in os.environ.items()}
        for field in self.__fields__.values():
            env_val: Optional[str] = None
            for env_name in field.field_info.extra['env_names']:
                env_val = env_vars.get(env_name)
                if env_val is not None:
                    break

            if env_val is None:
                continue

            if field.is_complex():
                try:
                    env_val = self.__config__.json_loads(env_val)  # type: ignore
                except ValueError as e:
                    raise SettingsError(f'error parsing JSON for "{env_name}"') from e
            d[field.alias] = env_val
        return d

    def _normalize_items(self, items: Dict[str, str]) -> Dict[str, str]:
        result = {}
        legal_names = [f.alias for f in self.__fields__.values()]
        legal_lower_names = [val.lower() for val in legal_names]
        relaxed_extra = self.__config__.extra in (Extra.allow, Extra.ignore)
        for name, value in items.items():
            if value is None:
                continue
            elif self.__config__.case_sensitive:
                if name in legal_names or relaxed_extra:
                    new_name = name
                else:
                    continue
            else:
                new_name = name.lower()
                if not (new_name in legal_lower_names or relaxed_extra):
                    continue
            new_name = new_name.replace(self.__config__.env_prefix, '')
            result[new_name] = value
        return result

    class Config:
        env_prefix = ''
        validate_all = True
        extra = Extra.forbid
        arbitrary_types_allowed = True
        case_sensitive = False
        additional_getters: Optional[
            List[Union[Callable[..., Dict[str, str]], Tuple[Callable[..., Dict[str, str]], Dict[str, Any]]]]
        ] = None
        # additional_getters = [
        #     (read_env_file, Path(".env")),
        #     (read_filesystem_directory, Path("/run/secrets"))
        # ]

        @classmethod
        def prepare_field(cls, field: ModelField) -> None:
            env_names: Union[List[str], AbstractSet[str]]
            env = field.field_info.extra.get('env')
            if env is None:
                if field.has_alias:
                    warnings.warn(
                        'aliases are no longer used by BaseSettings to define which environment variables to read. '
                        'Instead use the "env" field setting. '
                        'See https://pydantic-docs.helpmanual.io/usage/settings/#environment-variable-names',
                        FutureWarning,
                    )
                env_names = {cls.env_prefix + field.name}
            elif isinstance(env, str):
                env_names = {env}
            elif isinstance(env, (set, frozenset)):
                env_names = env
            elif sequence_like(env):
                env_names = list(env)
            else:
                raise TypeError(f'invalid field env: {env!r} ({display_as_type(env)}); should be string, list or set')

            if not cls.case_sensitive:
                env_names = env_names.__class__(n.lower() for n in env_names)
            field.field_info.extra['env_names'] = env_names

    __config__: Config  # type: ignore
