import os
import re
import json
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
from multiprocessing import get_context
from multiprocessing.pool import Pool
from multiprocessing.pool import ThreadPool
from numbers import Number
from typing import Any

import geopandas as gpd

from configs import vars_globals as gl
from functions.base_logger import WriteLogger


logger = WriteLogger(name=__name__)


def generate_id():
    return uuid.uuid4().hex
    

class JsonConfig(dict):
    """Custom dictionary; retrieves and updates values
    using key-dot notation. Modified behavior:
        >>> data = JsonConfig(**{
        ...     'globals': {
        ...         'date': {
        ...             'start': 1, 
        ...             'end': 2
        ...         }
        ...     }
        ... })
        >>> data['globals.date.start']        # -> Returns 1
        >>> data['globals.date.end'] = 20     # -> Replaces 2 with 20
        >>> data.get('globals.date', 'holy')  # -> Returns `default` if key is not present
    """
    def __getitem__(self, __key:str) -> Any:
        def _navegate_to_key(data:Any, keys:list):
            if not keys:
                return data
            key = keys[0]
            d = data[key]
            if not isinstance(d, dict):
                if len(keys) > 1:
                    raise KeyError(f'Key={".".join(keys)} not available')
                return d
            if not keys[1:]:
                return d
            return _navegate_to_key(d, keys[1:])

        keys = __key.split('.')
        if len(keys) == 1:
            return super().__getitem__(__key)
        return _navegate_to_key(self, keys)

    def __setitem__(self, __key:str, __value:Any) -> None:
        def _update_key(data:Any, keys:list, value:Any):
            if not keys:
                return None
            key = keys[0]
            if not isinstance(data[key], dict):
                if len(keys) > 1:
                    raise KeyError(f'Key={".".join(keys)} not available')
                data[key] = value
                return None
            if not keys[1:]:
                data[key] = value
                return None
            return _update_key(data[key], keys[1:], value)
        keys = __key.split('.')
        if len(keys) == 1:
            return super().__setitem__(__key, __value)
        return _update_key(data=self, keys=keys, value=__value)
            
    def get(self, key:str, default:Any=None) -> Any:
        try:
            return self[key]
        except Exception:
            return default


_convertions_tables = {
    'data': {
        'reference': 'bytes',
        'convertions': {
            'bytes': 1,
            'Kb': 1024,
            'Mb': 1024 * 1024,
            'Gb': 1024 * 1024 * 1024
        }
    },
    'data_rate': {
        'reference': 'bytes/sec',
        'convertions': {
            'bytes/sec': 1,
            'Kb/sec': 1024,
            'Mb/sec': 1024 * 1024,
            'Gb/sec': 1024 * 1024 * 1024
        }
    },
    'time': {
        'reference': 'sec',
        'convertions': {
            'sec': 1,
            'min': 60,
            'hr': 60 * 60,
            'day': 60 * 60 * 24
        }
    }
}


class Unit:

    def __init__(self, value:Number, unit:str):
        self.value = value
        self.unit = unit
        self.reference = self._find_reference()

    def _find_reference(self):
        ref = [v for v in _convertions_tables.values() if self.unit in v['convertions']]
        if not ref:
            raise KeyError(f'Cannot find unit={self.unit} in {_convertions_tables.values()}')
        ref = ref[0]
        reference_value = ref['convertions'][self.unit]
        return self.value * reference_value

    def _get_type(self, obj:Any):
        if not isinstance(obj, Unit):
            if not isinstance(obj, (int, float)):
                raise TypeError(f'Unsupported type to process for Unit class; provided={type(obj)}, expected=[Unit, int, float]')
            return obj
        return obj.reference

    def __add__(self, other:Any):
        v = self._get_type(other)
        return self.reference + v

    def __sub__(self, other:Any):
        v = self._get_type(other)
        return self.reference - v

    def __mul__(self, other:Any):
        v = self._get_type(other)
        return self.reference * v

    def __truediv__(self, other:Any):
        v = self._get_type(other)
        return self.reference / v

    def __index__(self):
        return int(self.reference)

    def __int__(self):
        return int(self.reference)

    def __float__(self):
        return float(self.reference)

    def __repr__(self):
        return f'{self.value:.4f} {self.unit}'


class UnitConverter:

    def __init__(self, type_unit:str='data'):
        self.reference = _convertions_tables[type_unit]['reference']
        self._convs = _convertions_tables[type_unit]['convertions'].copy()
        self._mappings = self._keys_mapping()

        if not self._convs[self.reference] == 1:
            self._convs[self.reference] = 1

        self._convs_dict_inv = dict(zip(self._convs.values(), self._convs.keys()))
        self._convs_dict_inv = {v : self._convs_dict_inv[v] for v in sorted(self._convs.values())}
        self._convs_dict_inv = OrderedDict(self._convs_dict_inv)

    def _keys_mapping(self):
        self._mappings = {key.lower() : key for key in self._convs}
        return self._mappings

    def _is_unit_in_convs(self, key:str):
        is_valid_unit = True
        if key not in self._mappings:
            is_valid_unit = False
        return is_valid_unit

    def convert(self, value:Number, from_unit:str, to_unit:str):
        from_key = from_unit.lower()
        to_key = to_unit.lower()
        try:
            if not self._is_unit_in_convs(from_key):
                raise KeyError(f'Unit from_unit=`{from_unit}` cannot be found; Must be one of these: {list(self._convs.keys())}')
            if not self._is_unit_in_convs(to_key):
                raise KeyError(f'Unit to_convert=`{to_unit}` cannot be found; Must be one of these: {list(self._convs.keys())}')
        except Exception as exception:
            raise exception
        v = value * self._convs[self._mappings[from_key]]
        convertion = self._convs[self._mappings[to_key]]
        result = v / convertion
        logger(f'{value:.2f} {from_unit} = {result:.2f} {to_unit}', level='debug')
        return Unit(result, to_unit)

    def get_unit_from_string(self, s:str):
        split = s.split()
        try:
            if len(split) != 2:
                raise ValueError(f'String must contain this patern: `<value> <unit>`; string={s}')
            value = float(split[0])
            unit = split[-1]
            key = unit.lower()
            if not self._is_unit_in_convs(key):
                raise ValueError(f'Unit `{unit}` cannot be found; Must be one of these: {list(self._convs.keys())}')
        except Exception as exc:
            raise exc
        return Unit(value, unit)

    def convert_smart(self, value:Number):
        def _convert(value, unit):
            value = value / self._convs[unit]            
            return value

        bigger = [k for k in self._convs_dict_inv.keys() if value >= k and k > 1]
        if not bigger:
            return Unit(value, self.reference)
        new_unit = self._convs_dict_inv[bigger[-1]] 
        v = _convert(value, new_unit)
        logger(f'{value:.2f} {self.reference} = {v:.2f} {new_unit}', level='debug')
        return Unit(v, new_unit)


class Timer:

    convs = UnitConverter(type_unit='time')

    def __init__(self):
        self.run_time = 0

    def time(self, func:callable):
        @wraps(func)
        def wrapper_timer(*args, **kwargs):
            init = time.perf_counter()
            value = func(*args, **kwargs)
            endt = time.perf_counter()
            self.run_time = self.convs.convert_smart(endt - init)
            logger(f'Finished [{func.__name__}] in {self.run_time}')
            return value
        return wrapper_timer

    def __enter__(self):
        logger(f'Profiling code inside context manager....', level='debug')
        self.run_time = time.perf_counter()
        return self
     
    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type:
            logger(f'Exiting from context manager with an error', level='debug')
            return None
        endt = time.perf_counter()
        self.run_time = self.convs.convert_smart(endt - self.run_time)
        logger(f'Finished code inside context manager in {self.run_time}')
        return None

    def __call__(self, message:str, level:str='info'):
        logger(message, level=level)
        return self


def load_environment_variable(name:str, load_from_environ:bool=False) -> str:
    value = name
    if load_from_environ:
        value = os.getenv(name)
        if value is None:
            raise KeyError(f'Environement variable={name} not available')
    return value


def load_json_config(path:str) -> JsonConfig:
    logger(f'Loading JSON configuration file at: {path}')
    with open(path) as file:
        config = json.load(file)
    logger(config, level='debug')
    return JsonConfig(**config)


def load_coordinates() -> gpd.GeoDataFrame:
    logger(f'Loading coordinates file at: {gl.COORDINATES_FILE}')
    if not os.path.exists(gl.COORDINATES_FILE):
        raise FileNotFoundError(f'File={gl.COORDINATES_FILE} not available')
    return gpd.read_file(gl.COORDINATES_FILE)


def get_batch_idxs(iterable:Any, batch_size:int) -> list:
    n = len(iterable)
    logger(f'Getting batch indices with N={n} and batch_size={batch_size}', level='debug')
    remain = n % batch_size
    if remain != 0:
        n += (batch_size - (n % batch_size))
    n += 1    
    logger(f'Fixed N={n}', level='debug')

    idxs = []
    values = list(range(0, n, batch_size))
    for idx, value in enumerate(values):
        if idx + 1 == len(values):
            break
        idxs.append((value, values[idx+1]))
    logger(f'Total of batches to process={len(idxs)}')
    return idxs


_pools_classes = [
    Pool, 
    ProcessPoolExecutor,
    ThreadPool,
    ThreadPoolExecutor
]
_pool_methods = [
    'map',
    'imap',
    'starmap'
]
_local_vars = locals()


def parallel_process(
        func:callable,
        iterable:Any,
        class_pool:str='Pool',
        pool_method:str='map',
        process:int=os.cpu_count(),
        context:str=None,
        timeout:int=None,
        chunksize=1,
        callable_if_timeout:callable=None
    ) -> Any:
    if isinstance(class_pool, str):
        if class_pool not in _local_vars:
            raise ValueError(f'Class pool={class_pool} is not available; must be one of these:{_pools_classes}')
        class_pool = _local_vars[class_pool]
    else:
        if class_pool not in _pools_classes:
            raise ValueError(f'Class pool={class_pool} is not available; must be one of these:{_pools_classes}')
    logger(f'Applying parallel process with Executor={class_pool}.{pool_method}(); process={process}', level='debug')
    
    kwargs = {}
    if context is not None:
        if class_pool == Pool:
            kwargs['context'] = get_context(context)
        elif class_pool == ProcessPoolExecutor:
            kwargs['mp_context'] = get_context(context)
       
    with class_pool(process, **kwargs) as pool:
        if pool_method not in _pool_methods:
            raise ValueError(f'Pool function={pool_method} is not available; available={_pool_methods}')
        if pool_method not in dir(pool):
            raise ValueError(f'Pool function={pool_method} is not available for class pool={class_pool}')
        pool_method = getattr(pool, pool_method)
        if timeout is not None and (class_pool == ProcessPoolExecutor or class_pool == ThreadPoolExecutor):
            if not isinstance(timeout, int):
                raise ValueError(f'Timout parameter must be integer: provided={type(timeout)}')
            logger(f'Setting pool timeout; timeout={timeout}', level='debug')
            features = pool_method(func, iterable, 
                        chunksize=chunksize, timeout=timeout)
            try:
                data = []
                for feature in features:
                    data.append(feature)
            except Exception as exc:
                # Cannot pass an optional raise_error flag because it will
                # not break the pool and it will continue
                logger(f'`TIMEOUT={timeout} sec` exceeded for function; leaving pool', level='error')
                if callable_if_timeout is not None:
                    try:
                        logger(f'Calling auxiliary function={callable_if_timeout} when timeout pool is exceeded')
                        callable_if_timeout()
                        logger(f'Calling auxiliary function={callable_if_timeout} done successfully')
                    except Exception as _exc:
                        logger(f'Callable if timeout return an RuntimeError; full traceback is show below:', level='error')
                        logger.exception(_exc)
                raise exc
            data = [d for d in data]
        else:
            data = pool_method(func, iterable, chunksize=chunksize)
            if class_pool == ProcessPoolExecutor or class_pool == ThreadPoolExecutor:
                data = list(data)
        logger(f'Parallel process done successfully', level='debug')
        return data


def replace_templates(value:str, replace_values:dict,  template:str='<...>'):
    logger(f'Value before replace templates: {value}', level='debug')    

    _valid_templates = ['<...>']
    if not isinstance(value, str):
        raise ValueError(f'Input value must be a string: provided={type(value)}')
    if not isinstance(replace_values, dict):
        raise ValueError(f'Replace values must be a dictionary; provided={type(replace_values)}')
    if not isinstance(template, str):
        raise ValueError(f'Template must be a string: provided={type(template)}')
    
    if template not in _valid_templates:
        raise ValueError(f'Invalid template format, template must be one of these:{_valid_templates}')
    t1, t2 = template.split('...')
    
    # TODO: Modify regex for mor templates added in _valid_templates
    regex = r'\{}(.*?)\{}'.format(t1, t2)
    templates = re.findall(regex, value)
    
    for template in templates:
        dateformat = re.findall(r'\((.*?)\)', template)
        if dateformat:
            replace = replace_values.get(template.replace(f'({dateformat[0]})', ''))
        else:
            replace = replace_values.get(template)
        
        if replace is None:
            logger(f'Cannot find template={template} in replace_values keys: available={replace_values.keys()}', level='warning')
            continue

        _template = f'{t1}{template}{t2}'
        if isinstance(replace, datetime):
            if not dateformat:
                raise ValueError(f'Dateformat not found; cannot convert date to string: Parse the dateformat after the template key beetwen parenthesis: <MY-TEMPLATE-KEY(%Y-%m-%dT%H:%M:%SZ)>')
            value = value.replace(_template, replace.strftime(dateformat[0]))
        else:
            value = value.replace(_template, str(replace))

    logger(f'Value after replace templates: {value}', level='debug')
    return value
