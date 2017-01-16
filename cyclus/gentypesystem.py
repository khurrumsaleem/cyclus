#!/usr/bin/env python
"""Generates Cyclus Type System bindings.

Module history:

- 2016-10-12: scopatz: This file used to be called genapi.py in cymetric.
"""
from __future__ import print_function, unicode_literals

import io
import os
import sys
import imp
import json
import argparse
import platform
import warnings
import itertools
import subprocess
from glob import glob
from distutils import core, dir_util
from pprint import pprint, pformat
if sys.version_info[0] > 2:
    from urllib.request import urlopen
    str_types = (str, bytes)
else:
    from urllib2 import urlopen
    str_types = (str, unicode)

import jinja2

#
# Type System
#

class TypeSystem(object):
    """A type system for cyclus code generation."""

    def __init__(self, table, cycver, rawver=None, cpp_typesystem='cpp_typesystem'):
        """Parameters
        ----------
        table : list
            A table of possible types. The first row must be the column names.
        cycver : tuple of ints
            Cyclus version number.
        rawver : string, optional
            A full, raw version string, if available
        cpp_typesystem : str, optional
            The namespace of the C++ wrapper header.

        Attributes
        ----------
        table : list
            A stripped down table of type information.
        cols : dict
            Maps column names to column number in table.
        cycver : tuple of ints
            Cyclus version number.
        verstr : str
            A version string of the format 'vX.X'.
        types : set of str
            The type names in the type system.
        ids : dict
            Maps types to integer identifier.
        cpptypes : dict
            Maps types to C++ type.
        ranks : dict
            Maps types to shape rank.
        norms : dict
            Maps types to programmatic normal form, ie INT -> 'int' and
            VECTOR_STRING -> ('std::vector', 'std::string').
        dbtypes : list of str
            The type names in the type system, sorted by id.
        uniquetypes : list of str
            The type names in the type system, sorted by id,
            which map to a unique C++ type.
        """
        self.cpp_typesystem = cpp_typesystem
        self.cycver = cycver
        self.rawver = rawver
        self.verstr = verstr = 'v{0}.{1}'.format(*cycver)
        self.cols = cols = {x: i for i, x in enumerate(table[0])}
        id, name, version = cols['id'], cols['name'], cols['version']
        cpptype, rank = cols['C++ type'], cols['shape rank']
        tab = []
        if rawver is not None:
            tab = [row for row in table if row[version] == rawver]
        if len(tab) == 0:
            tab = [row for row in table if row[version].startswith(verstr)]
            if len(tab) == 0:
                raise ValueError("Cyclus version could not be found in table!")
        self.table = table = tab
        self.types = types = set()
        self.ids = ids = {}
        self.cpptypes = cpptypes = {}
        self.ranks = ranks = {}
        for row in table:
            t = row[name]
            types.add(t)
            ids[t] = row[id]
            cpptypes[t] = row[cpptype]
            ranks[t] = row[rank]
        self.norms = {t: parse_template(c) for t, c in cpptypes.items()}
        self.dbtypes = sorted(types, key=lambda t: ids[t])
        # find unique types
        seen = set()
        self.uniquetypes = uniquetypes = []
        for t in self.dbtypes:
            normt = self.norms[t]
            if normt in seen:
                continue
            else:
                uniquetypes.append(t)
                seen.add(normt)
        self.resources = tuple(RESOURCES)
        self.inventory_types = inventory_types = []
        for t in uniquetypes:
            normt = self.norms[t]
            if normt in INVENTORIES or normt[0] in INVENTORIES:
                inventory_types.append(t)

        # caches
        self._cython_cpp_name = {}
        self._cython_types = dict(CYTHON_TYPES)
        self._funcnames = dict(FUNCNAMES)
        self._classnames = dict(CLASSNAMES)
        self._vars_to_py = dict(VARS_TO_PY)
        self._vars_to_cpp = dict(VARS_TO_CPP)
        self._nptypes = dict(NPTYPES)
        self._to_py_converters = dict(TO_PY_CONVERTERS)
        self._to_cpp_converters = dict(TO_CPP_CONVERTERS)

    def cython_cpp_name(self, t):
        """Returns the C++ name of the type, eg INT -> cpp_typesystem.INT."""
        if t not in self._cython_cpp_name:
            self._cython_cpp_name[t] = '{0}.{1}'.format(self.cpp_typesystem, t)
        return self._cython_cpp_name[t]

    def cython_type(self, t):
        """Returns the Cython spelling of the type."""
        if t in self._cython_types:
            return self._cython_types[t]
        if isinstance(t, str_types):
            n = self.norms[t]
            return self.cython_type(n)
        # must be teplate type
        cyt = list(map(self.cython_type, t))
        cyt = '{0}[{1}]'.format(cyt[0], ', '.join(cyt[1:]))
        self._cython_types[t] = cyt
        return cyt

    def funcname(self, t):
        """Returns a version of the type name suitable for use in a function name.
        """
        if t in self._funcnames:
            return self._funcnames[t]
        if isinstance(t, str_types):
            n = self.norms[t]
            return self.funcname(n)
        f = '_'.join(map(self.funcname, t))
        self._funcnames[t] = f
        return f

    def classname(self, t):
        """Returns a version of the type name suitable for use in a class name.
        """
        if t in self._classnames:
            return self._classnames[t]
        if isinstance(t, str_types):
            n = self.norms[t]
            return self.classname(n)
        c = ''.join(map(self.classname, t))
        self._classnames[t] = c
        return c

    def var_to_py(self, x, t):
        """Returns an expression for converting an object to Python."""
        n = self.norms.get(t, t)
        expr = self._vars_to_py.get(n, None)
        if expr is None:
            f = self.funcname(t)
            expr = f + '_to_py({var})'
            self._vars_to_py[n] = expr
        return expr.format(var=x)

    def hold_any_to_py(self, x, t):
        """Returns an expression for converting a hold_any object to Python."""
        cyt = self.cython_type(t)
        cast = '{0}.cast[{1}]()'.format(x, cyt)
        return self.var_to_py(cast, t)

    def var_to_cpp(self, x, t):
        """Returns an expression for converting a Python object to C++."""
        n = self.norms.get(t, t)
        expr = self._vars_to_cpp.get(n, None)
        if expr is None:
            f = self.funcname(t)
            expr = f + '_to_cpp({var})'
            self._vars_to_cpp[n] = expr
        return expr.format(var=x)

    def py_to_any(self, a, val, t):
        """Returns an expression for assigning a Python object (val) to an any
        object (a)."""
        cyt = self.cython_type(t)
        cpp = self.var_to_cpp(val, t)
        rtn = '{a}.assign[{cyt}]({cpp})'.format(a=a, cyt=cyt, cpp=cpp)
        return rtn

    def nptype(self, n):
        """Returns the numpy type for a normal form element."""
        npt = self._nptypes.get(n, None)
        if npt is None:
            npt = 'np.NPY_OBJECT'
            self._nptypes[n] = npt
        return npt

    def convert_to_py(self, x, t):
        """Converts a C++ variable to python.

        Parameters
        ----------
        x : str
            variable name
        t : str
            variable type

        Returns
        -------
        decl : str
            Declarations needed for conversion, may be many lines.
        body : str
            Actual conversion implementation, may be many lines.
        rtn : str
            Return expression.
        """
        n = self.norms.get(t, t)
        ctx = {'type': self.cython_type(t), 'var': x, 'nptypes': []}
        if n in self._to_py_converters:
            # basic type or str
            n0 = ()
            decl, body, expr = self._to_py_converters[n]
        elif n[0] == 'std::vector' and self.nptype(n[1]) == 'np.NPY_OBJECT':
            # vector of type that should become an object
            n0 = n[0]
            decl, body, expr = self._to_py_converters['np.ndarray', 'np.NPY_OBJECT']
            ctx['elem_to_py'] = self.var_to_py(x + '[i]', n[1])
        else:
            # must be a template already
            n0 = n[0]
            decl, body, expr = self._to_py_converters[n0]
        for targ, n_i in zip(TEMPLATE_ARGS.get(n0, ()), n[1:]):
            x_i = x + '_' +  targ
            ctx[targ+'name'] = x_i
            ctx[targ+'type'] = self.cython_type(n_i)
            dbe_i = self.convert_to_py(x_i, n_i)
            dbe_i = map(Indenter, dbe_i)
            ctx[targ+'decl'], ctx[targ+'body'], ctx[targ+'expr'] = dbe_i
            ctx['nptypes'].append(self.nptype(n_i))
        decl = decl.format(**ctx)
        body = body.format(**ctx)
        expr = expr.format(**ctx)
        return decl, body, expr

    def convert_to_cpp(self, x, t):
        """Converts a Python variable to C++.

        Parameters
        ----------
        x : str
            variable name
        t : str
            variable type

        Returns
        -------
        decl : str
            Declarations needed for conversion, may be many lines.
        body : str
            Actual conversion implementation, may be many lines.
        rtn : str
            Return expression.
        """
        n = self.norms.get(t, t)
        ctx = {'type': self.cython_type(t), 'var': x, 'nptypes': []}
        if n in self._to_cpp_converters:
            # basic type or str
            n0 = ()
            decl, body, expr = self._to_cpp_converters[n]
        elif n[0] == 'std::vector' and self.nptype(n[1]) in ('np.NPY_OBJECT',
                                                             'np.NPY_BOOL'):
            # vector of type that should become an object
            n0 = n[0]
            decl, body, expr = self._to_cpp_converters['np.ndarray', 'np.NPY_OBJECT']
        else:
            # must be a template already
            n0 = n[0]
            decl, body, expr = self._to_cpp_converters[n0]
        for targ, n_i in zip(TEMPLATE_ARGS.get(n0, ()), n[1:]):
            x_i = x + '_' +  targ
            ctx[targ+'name'] = x_i
            ctx[targ+'type'] = self.cython_type(n_i)
            dbe_i = self.convert_to_cpp(x_i, n_i)
            dbe_i = map(Indenter, dbe_i)
            ctx[targ+'decl'], ctx[targ+'body'], ctx[targ+'expr'] = dbe_i
            ctx['nptypes'].append(self.nptype(n_i))
            ctx[targ+'_to_cpp'] = self.var_to_cpp(x_i, n_i)
        decl = decl.format(**ctx)
        body = body.format(**ctx)
        expr = expr.format(**ctx)
        return decl, body, expr


CYTHON_TYPES = {
    # type system types
    'BOOL': 'cpp_bool',
    'INT': 'int',
    'FLOAT': 'float',
    'DOUBLE': 'double',
    'STRING': 'std_string',
    'VL_STRING': 'std_string',
    'BLOB': 'cpp_cyclus.Blob',
    'UUID': 'cpp_cyclus.uuid',
    'MATERIAL': 'cpp_cyclus.Material',
    'PRODUCT': 'cpp_cyclus.Product',
    'RESOURCE_BUFF': 'cpp_cyclus.ResourceBuff',
    # C++ normal types
    'bool': 'cpp_bool',
    'int': 'int',
    'float': 'float',
    'double': 'double',
    'std::string': 'std_string',
    'std::string': 'std_string',
    'cyclus::Blob': 'cpp_cyclus.Blob',
    'boost::uuids::uuid': 'cpp_cyclus.uuid',
    'cyclus::Material': 'cpp_cyclus.Material',
    'cyclus::Product': 'cpp_cyclus.Product',
    'cyclus::toolkit::ResourceBuff': 'cpp_cyclus.ResourceBuff',
    # Template Types
    'std::set': 'std_set',
    'std::map': 'std_map',
    'std::pair': 'std_pair',
    'std::list': 'std_list',
    'std::vector': 'std_vector',
    'cyclus::toolkit::ResBuf': 'cpp_cyclus.ResBuf',
    'cyclus::toolkit::ResMap': 'cpp_cyclus.ResMap',
    }

# Don't include the base resource class here since it is pure virtual.
RESOURCES = ['MATERIAL', 'PRODUCT']

INVENTORIES = ['cyclus::toolkit::ResourceBuff', 'cyclus::toolkit::ResBuf',
               'cyclus::toolkit::ResMap']

FUNCNAMES = {
    # type system types
    'BOOL': 'bool',
    'INT': 'int',
    'FLOAT': 'float',
    'DOUBLE': 'double',
    'STRING': 'std_string',
    'VL_STRING': 'std_string',
    'BLOB': 'blob',
    'UUID': 'uuid',
    'MATERIAL': 'material',
    'PRODUCT': 'product',
    'RESOURCE_BUFF': 'resource_buff',
    # C++ normal types
    'bool': 'bool',
    'int': 'int',
    'float': 'float',
    'double': 'double',
    'std::string': 'std_string',
    'cyclus::Blob': 'blob',
    'boost::uuids::uuid': 'uuid',
    'cyclus::Material': 'material',
    'cyclus::Product': 'product',
    'cyclus::toolkit::ResourceBuff': 'resource_buff',
    # Template Types
    'std::set': 'std_set',
    'std::map': 'std_map',
    'std::pair': 'std_pair',
    'std::list': 'std_list',
    'std::vector': 'std_vector',
    'cyclus::toolkit::ResBuf': 'res_buf',
    'cyclus::toolkit::ResMap': 'res_map',
    }

CLASSNAMES = {
    # type system types
    'BOOL': 'Bool',
    'INT': 'Int',
    'FLOAT': 'Float',
    'DOUBLE': 'Double',
    'STRING': 'String',
    'VL_STRING': 'String',
    'BLOB': 'Blob',
    'UUID': 'Uuid',
    'MATERIAL': 'Material',
    'PRODUCT': 'Product',
    'RESOURCE_BUFF': 'ResourceBuff',
    # C++ normal types
    'bool': 'Bool',
    'int': 'Int',
    'float': 'Float',
    'double': 'Double',
    'std::string': 'String',
    'cyclus::Blob': 'Blob',
    'boost::uuids::uuid': 'Uuid',
    'cyclus::Material': 'Material',
    'cyclus::Product': 'Product',
    'cyclus::toolkit::ResourceBuff': 'ResourceBuff',
    # Template Types
    'std::set': 'Set',
    'std::map': 'Map',
    'std::pair': 'Pair',
    'std::list': 'List',
    'std::vector': 'Vector',
    'cyclus::toolkit::ResBuf': 'ResBuf',
    'cyclus::toolkit::ResMap': 'ResMap',
    }



# note that this maps normal forms to python
VARS_TO_PY = {
    'bool': '{var}',
    'int': '{var}',
    'float': '{var}',
    'double': '{var}',
    'std::string': 'bytes({var}).decode()',
    'cyclus::Blob': 'blob_to_bytes({var})',
    'boost::uuids::uuid': 'uuid_cpp_to_py({var})',
    'cyclus::Material': 'None',
    'cyclus::Product': 'None',
    'cyclus::toolkit::ResourceBuff': 'None',
    }

# note that this maps normal forms to python
VARS_TO_CPP = {
    'bool': '<bint> {var}',
    'int': '<int> {var}',
    'float': '<float> {var}',
    'double': '<double> {var}',
    'std::string': 'str_py_to_cpp({var})',
    'cyclus::Blob': 'cpp_cyclus.Blob(std_string(<const char*> {var}))',
    'boost::uuids::uuid': 'uuid_py_to_cpp({var})',
    'cyclus::Material': 'None',
    'cyclus::Product': 'None',
    'cyclus::toolkit::ResourceBuff': 'None',
    }

TEMPLATE_ARGS = {
    'std::set': ('val',),
    'std::map': ('key', 'val'),
    'std::pair': ('first', 'second'),
    'std::list': ('val',),
    'std::vector': ('val',),
    'cyclus::toolkit::ResBuf': ('val',),
    'cyclus::toolkit::ResMap': ('key', 'val'),
    }

NPTYPES = {
    'bool': 'np.NPY_BOOL',
    'int': 'np.NPY_INT32',
    'float': 'np.NPY_FLOAT32',
    'double': 'np.NPY_FLOAT64',
    'std::string': 'np.NPY_OBJECT',
    'cyclus::Blob': 'np.NPY_OBJECT',
    'boost::uuids::uuid': 'np.NPY_OBJECT',
    'cyclus::Material': 'None',
    'cyclus::Product': 'None',
    'cyclus::toolkit::ResourceBuff': 'None',
    'std::set': 'np.NPY_OBJECT',
    'std::map': 'np.NPY_OBJECT',
    'std::pair': 'np.NPY_OBJECT',
    'std::list': 'np.NPY_OBJECT',
    'std::vector': 'np.NPY_OBJECT',
    'cyclus::toolkit::ResBuf': 'np.NPY_OBJECT',
    'cyclus::toolkit::ResMap': 'np.NPY_OBJECT',
    }

# note that this maps normal forms to python
TO_PY_CONVERTERS = {
    # base types
    'bool': ('', '', '{var}'),
    'int': ('', '', '{var}'),
    'float': ('', '', '{var}'),
    'double': ('', '', '{var}'),
    'std::string': ('\n', '\npy{var} = {var}\npy{var} = py{var}.decode()\n',
                    'py{var}'),
    'cyclus::Blob': ('', '', 'blob_to_bytes({var})'),
    'boost::uuids::uuid': ('', '', 'uuid_cpp_to_py({var})'),
    'cyclus::Material': ('', '', 'None'),
    'cyclus::Product': ('', '', 'None'),
    'cyclus::toolkit::ResourceBuff': ('', '', 'None'),
    # templates
    'std::set': (
        '{valdecl}\n'
        'cdef {valtype} {valname}\n'
        'cdef std_set[{valtype}].iterator it{var}\n'
        'cdef set py{var}\n',
        'py{var} = set()\n'
        'it{var} = {var}.begin()\n'
        'while it{var} != {var}.end():\n'
        '    {valname} = deref(it{var})\n'
        '    {valbody.indent4}\n'
        '    pyval = {valexpr}\n'
        '    py{var}.add(pyval)\n'
        '    inc(it{var})\n',
        'py{var}'),
    'std::map': (
        '{keydecl}\n'
        '{valdecl}\n'
        'cdef {keytype} {keyname}\n'
        'cdef {valtype} {valname}\n'
        'cdef {type}.iterator it{var}\n'
        'cdef dict py{var}\n',
        'py{var} = {{}}\n'
        'it{var} = {var}.begin()\n'
        'while it{var} != {var}.end():\n'
        '    {keyname} = deref(it{var}).first\n'
        '    {keybody.indent4}\n'
        '    pykey = {keyexpr}\n'
        '    {valname} = deref(it{var}).second\n'
        '    {valbody.indent4}\n'
        '    pyval = {valexpr}\n'
        '    py{var}[pykey] = pyval\n'
        '    inc(it{var})\n',
        'py{var}'),
    'std::pair': (
        '{firstdecl}\n'
        '{seconddecl}\n'
        'cdef {firsttype} {firstname}\n'
        'cdef {secondtype} {secondname}\n',
        '{firstname} = {var}.first\n'
        '{firstbody}\n'
        'pyfirst = {firstexpr}\n'
        '{secondname} = {var}.second\n'
        '{secondbody}\n'
        'pysecond = {secondexpr}\n'
        'py{var} = (pyfirst, pysecond)\n',
        'py{var}'),
    'std::list': (
        '{valdecl}\n'
        'cdef {valtype} {valname}\n'
        'cdef std_list[{valtype}].iterator it{var}\n'
        'cdef list py{var}\n',
        'py{var} = []\n'
        'it{var} = {var}.begin()\n'
        'while it{var} != {var}.end():\n'
        '    {valname} = deref(it{var})\n'
        '    {valbody.indent4}\n'
        '    pyval = {valexpr}\n'
        '    py{var}.append(pyval)\n'
        '    inc(it{var})\n',
        'py{var}'),
    'std::vector': (
        'cdef np.npy_intp {var}_shape[1]\n',
        '{var}_shape[0] = <np.npy_intp> {var}.size()\n'
        'py{var} = np.PyArray_SimpleNewFromData(1, {var}_shape, {nptypes[0]}, '
            '&{var}[0])\n'
        'py{var} = np.PyArray_Copy(py{var})\n',
        'py{var}'),
    ('std::vector', 'bool'): (
        'cdef int i\n'
        'cdef np.npy_intp {var}_shape[1]\n',
        '{var}_shape[0] = <np.npy_intp> {var}.size()\n'
        'py{var} = np.PyArray_SimpleNew(1, {var}_shape, np.NPY_BOOL)\n'
        'for i in range({var}_shape[0]):\n'
        '    py{var}[i] = {var}[i]\n',
        'py{var}'),
    ('np.ndarray', 'np.NPY_OBJECT'): (
        'cdef int i\n'
        'cdef np.npy_intp {var}_shape[1]\n',
        '{var}_shape[0] = <np.npy_intp> {var}.size()\n'
        'py{var} = np.PyArray_SimpleNew(1, {var}_shape, np.NPY_OBJECT)\n'
        'for i in range({var}_shape[0]):\n'
        '    {var}_i = {elem_to_py}\n'
        '    py{var}[i] = {var}_i\n',
        'py{var}'),
    'cyclus::toolkit::ResBuf': ('', '', 'None'),
    'cyclus::toolkit::ResMap': ('', '', 'None'),
    }

TO_CPP_CONVERTERS = {
    # base types
    'bool': ('', '', '<bint> {var}'),
    'int': ('', '', '<int> {var}'),
    'float': ('', '', '<float> {var}'),
    'double': ('', '', '<double> {var}'),
    'std::string': ('cdef bytes b_{var}',
        'if isinstance({var}, str):\n'
        '   b_{var} = {var}.encode()\n'
        'elif isinstance({var}, str):\n'
        '   b_{var} = {var}\n'
        'else:\n'
        '   b_{var} = bytes({var})\n',
        'std_string(<const char*> b_{var})'),
    'cyclus::Blob': ('', '', 'cpp_cyclus.Blob(std_string(<const char*> {var}))'),
    'boost::uuids::uuid': ('', '', 'uuid_py_to_cpp({var})'),
    'cyclus::Material': ('', '', 'None'),
    'cyclus::Product': ('', '', 'None'),
    'cyclus::toolkit::ResourceBuff': ('', '', 'None'),
    # templates
    'std::set': (
        '{valdecl}\n'
        'cdef std_set[{valtype}] cpp{var}\n',
        'for {valname} in {var}:\n'
        '    {valbody.indent4}\n'
        '    cpp{var}.insert({valexpr})\n',
        'cpp{var}'),
    'std::map': (
        '{keydecl}\n'
        '{valdecl}\n'
        'cdef {type} cpp{var}\n',
        'if not isinstance({var}, collections.Mapping):\n'
        '    {var} = dict({var})\n'
        'for {keyname}, {valname} in {var}.items():\n'
        '    {keybody.indent4}\n'
        '    {valbody.indent4}\n'
        '    cpp{var}[{keyexpr}] = {valexpr}\n',
        'cpp{var}'),
    'std::pair': (
        '{firstdecl}\n'
        '{seconddecl}\n'
        'cdef {type} cpp{var}\n',
        '{firstname} = {var}[0]\n'
        '{firstbody}\n'
        'cpp{var}.first = {firstexpr}\n'
        '{secondname} = {var}[1]\n'
        '{secondbody}\n'
        'cpp{var}.second = {secondexpr}\n',
        'cpp{var}'),
    'std::list': (
        '{valdecl}\n'
        'cdef std_list[{valtype}] cpp{var}\n',
        'for {valname} in {var}:\n'
        '    {valbody.indent4}\n'
        '    cpp{var}.push_back({valexpr})\n',
        'cpp{var}'),
    'std::vector': (
        'cdef int i\n'
        'cdef int {var}_size\n'
        'cdef {type} cpp{var}\n'
        'cdef {valtype} * {var}_data\n',
        '{var}_size = len({var})\n'
        'if isinstance({var}, np.ndarray) and '
        '(<np.ndarray> {var}).descr.type_num == {nptypes[0]}:\n'
        '    {var}_data = <{valtype} *> np.PyArray_DATA(<np.ndarray> {var})\n'
        '    cpp{var}.resize(<size_t> {var}_size)\n'
        '    memcpy(<void*> &cpp{var}[0], {var}_data, sizeof({valtype}) * {var}_size)\n'
        'else:\n'
        '    for i, {valname} in enumerate({var}):\n'
        '        cpp{var}[i] = {val_to_cpp}\n',
        'cpp{var}'),
    ('np.ndarray', 'np.NPY_OBJECT'): (
        'cdef int i\n'
        'cdef int {var}_size\n'
        'cdef {type} cpp{var}\n',
        '{var}_size = len({var})\n'
        'cpp{var}.resize(<size_t> {var}_size)\n'
        'for i, {valname} in enumerate({var}):\n'
        '    cpp{var}[i] = {val_to_cpp}\n',
        'cpp{var}'),
    'cyclus::toolkit::ResBuf': ('', '', 'None'),
    'cyclus::toolkit::ResMap': ('', '', 'None'),
    }

# annotation info key (pyname), C++ name,  cython type names, init snippet
ANNOTATIONS = [
    ('type', 'type', 'object'),
    ('index', 'index', 'int'),
    ('default', 'dflt', 'object'),
    ('internal', 'internal', 'bint'),
    ('shape', 'shape', 'object'),
    ('doc', 'doc', 'str'),
    ('tooltip', 'tooltip', 'str'),
    ('units', 'units', 'str'),
    ('userlevel', 'userlevel', 'int'),
    ('alias', 'alias', 'object'),
    ('uilabel', 'uilabel', 'str'),
    ('uitype', 'uitype', 'object'),
    ('range', 'range', 'object'),
    ('categorical', 'categorical', 'object'),
    ('schematype', 'schematype', 'object'),
    ('initfromcopy', 'initfromcopy', 'str'),
    ('initfromdb', 'initfromdb', 'str'),
    ('infiletodb', 'infiletodb', 'str'),
    ('schema', 'schema', 'str'),
    ('snapshot', 'snapshot', 'str'),
    ('snapshotinv', 'snapshotinv', 'str'),
    ('initinv', 'initinv', 'str'),
    ('uniquetypeid', 'uniquetypeid', 'int'),
    ]


def split_template_args(s, open_brace='<', close_brace='>', separator=','):
    """Takes a string with template specialization and returns a list
    of the argument values as strings. Mostly cribbed from xdress.
    """
    targs = []
    ns = s.split(open_brace, 1)[-1].rsplit(close_brace, 1)[0].split(separator)
    count = 0
    targ_name = ''
    for n in ns:
        count += n.count(open_brace)
        count -= n.count(close_brace)
        if len(targ_name) > 0:
            targ_name += separator
        targ_name += n
        if count == 0:
            targs.append(targ_name.strip())
            targ_name = ''
    return targs


def parse_template(s, open_brace='<', close_brace='>', separator=','):
    """Takes a string -- which may represent a template specialization --
    and returns the corresponding type. Mostly cribbed from xdress.
    """
    if open_brace not in s and close_brace not in s:
        return s
    t = [s.split(open_brace, 1)[0]]
    targs = split_template_args(s, open_brace=open_brace,
                                close_brace=close_brace, separator=separator)
    for targ in targs:
        t.append(parse_template(targ, open_brace=open_brace,
                                close_brace=close_brace, separator=separator))
    t = tuple(t)
    return t


class Indenter(object):
    """Handles indentations."""
    def __init__(self, s):
        """Constructor for string object."""
        self._s = s

    def __str__(self):
        """Returns a string."""
        return self._s

    def __getattr__(self, key):
        """Replaces an indentation with a newline and spaces."""
        if key.startswith('indent'):
            n = int(key[6:])
            return self._s.replace('\n', '\n' + ' '*n)
        return self.__dict__[key]

def safe_output(cmd, shell=False, *args, **kwargs):
    """Checks that a command successfully runs with/without shell=True.
    Returns the output.
    """
    try:
        out = subprocess.check_output(cmd, shell=False, *args, **kwargs)
    except (subprocess.CalledProcessError, OSError):
        cmd = ' '.join(cmd)
        out = subprocess.check_output(cmd, shell=True, *args, **kwargs)
    return out

#
# Code Generation
#

JENV = jinja2.Environment(undefined=jinja2.StrictUndefined)

CG_WARNING = """
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!!!! WARNING - THIS FILE HAS BEEN !!!!!!
# !!!!!   AUTOGENERATED BY CYCLUS    !!!!!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
""".strip()

STL_CIMPORTS = """
# Cython standard library imports
from libcpp.map cimport map as std_map
from libcpp.set cimport set as std_set
from libcpp.list cimport list as std_list
from libcpp.vector cimport vector as std_vector
from libcpp.utility cimport pair as std_pair
from libcpp.string cimport string as std_string
from libcpp.typeinfo cimport type_info
from libcpp.memory cimport shared_ptr
from cython.operator cimport dereference as deref
from cython.operator cimport preincrement as inc
from cython.operator cimport typeid
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy
from libcpp cimport bool as cpp_bool
""".strip()

NPY_IMPORTS = """
# numpy imports & startup
cimport numpy as np
import numpy as np
np.import_array()
np.import_ufunc()
""".strip()

CPP_TYPESYSTEM = JENV.from_string("""
{{ cg_warning }}

cdef extern from "cyclus.h" namespace "cyclus":

    cdef enum DbTypes:
        {{ dbtypes | join('\n') | indent(8) }}

""".strip())

def cpp_typesystem(ts, ns):
    """Creates the Cython header that wraps the Cyclus type system."""
    ctx = dict(
        dbtypes=ts.dbtypes,
        cg_warning=CG_WARNING,
        stl_cimports=STL_CIMPORTS,
        )
    rtn = CPP_TYPESYSTEM.render(ctx)
    return rtn


TYPESYSTEM_PYX = JENV.from_string('''
{{ cg_warning }}

{{ stl_cimports }}

{{ npy_imports }}

# local imports
from cyclus cimport cpp_typesystem
from cyclus cimport cpp_cyclus
from cyclus cimport lib

# pure python imports
import uuid
import collections
from binascii import hexlify

from cyclus import nucname

#
# Resources & Inventories
#

cdef class _Resource:

    def __cinit__(self, bint init=False):
        self._free = init
        self.ptx = NULL

    def __dealloc__(self):
        """C++ destructor."""
        # Note that we have to do it this way since self.ptx is void*
        if self.ptx == NULL or not self._free:
            return
        cdef cpp_cyclus.Resource* cpp_ptx = <cpp_cyclus.Resource*> self.ptx
        del cpp_ptx
        self.ptx = NULL

    @property
    def obj_id(self):
        """The unique id corresponding to this resource object. Can be used
        to track and/or associate other information with this resource object.
        You should NOT track resources by pointer.
        """
        return (<cpp_cyclus.Resource*> self.ptx).obj_id()

    @property
    def state_id(self):
        """The unique id corresponding to this resource and its current
        state.  All resource id's are unique - even across different resource
        types/implementations. Runtime tracking of resources should generally
        use the obj_id rather than this.
        """
        return (<cpp_cyclus.Resource*> self.ptx).state_id()

    def bump_state_id(self):
        """Assigns a new, unique internal id to this resource and its state. This
        should be called by resource implementations whenever their state changes.
        A call to bump_state_id is not necessarily accompanied by a change to the
        state id. This should NEVER be called by agents.
        """
        (<cpp_cyclus.Resource*> self.ptx).BumpStateId()

    @property
    def qual_id(self):
        """Returns an id representing the specific resource implementation's internal
        state that is not accessible via the Resource class public interface.  Any
        change to the qual_id should always be accompanied by a call to
        bump_state_id().
        """
        return (<cpp_cyclus.Resource*> self.ptx).qual_id()

    @property
    def type(self):
        """A unique type/name for the concrete resource implementation."""
        t = std_string_to_py((<cpp_cyclus.Resource*> self.ptx).type())
        return t

    def clone(self):
        """Returns an untracked (not part of the simulation) copy of the resource.
        A cloned resource should never record anything in the output database.
        """
        cdef _Resource co = Resource()
        co._free = True
        co.ptx = <void*> (<cpp_cyclus.Resource*> self.ptx).Clone()
        copy = co
        return copy

    def record(self, lib._Context ctx):
        """Records the resource's state to the output database.  This method
        should generally NOT record data accessible via the Resource class
        public methods (e.g.  qual_id, units, type, quantity).
        """
        (<cpp_cyclus.Resource*> self.ptx).Record(ctx.ptx)

    @property
    def units(self):
        """Returns the units this resource is based in (e.g. "kg")."""
        return (<cpp_cyclus.Resource*> self.ptx).units()

    @property
    def quantity(self):
        """Returns the quantity of this resource with dimensions as specified by
        the return value of units().
        """
        return (<cpp_cyclus.Resource*> self.ptx).quantity()

    def extract_res(self, double quantity):
        """Splits the resource and returns the extracted portion as a new resource
        object.  Allows for things like ResBuf and Traders to split
        offers/requests of arbitrary resource implementation type.
        """
        cdef _Resource res = Resource()
        res._free = True
        res.ptx = <void*> (<cpp_cyclus.Resource*> self.ptx).ExtractRes(quantity)
        respy = res
        return respy


class Resource(_Resource):
    """Resource defines an abstract interface implemented by types that are
    offered, requested, and transferred between simulation agents. Resources
    represent the lifeblood of a simulation.
    """


cdef shared_ptr[cpp_cyclus.Composition] composition_ptr_from_py(object comp,
                                                                object basis):
    """Converts a dict-like to a composition."""
    if not isinstance(comp, dict):
        comp = dict(comp)
    cdef int k
    cdef double v
    cdef cpp_cyclus.CompMap c
    for key, val in comp.items():
        k = nucname.id(key)
        v = val
        c[k] = v
    cdef shared_ptr[cpp_cyclus.Composition] p
    if basis == 'mass':
        p = cpp_cyclus.Composition.CreateFromMass(c)
    elif basis == 'atom':
        p = cpp_cyclus.Composition.CreateFromAtom(c)
    else:
        raise ValueError('Composition basis must be either mass or atom, '
                         'not ' + str(basis))
    return p


cdef object composition_from_cpp(shared_ptr[cpp_cyclus.Composition] comp, object basis):
    """Converts a composition to a dict."""
    cdef cpp_cyclus.CompMap c
    if basis == 'mass':
        c = deref(comp).mass()
    elif basis == 'atom':
        c = deref(comp).atom()
    else:
        raise ValueError('Composition basis must be either mass or atom, '
                         'not ' + str(basis))
    rtn = std_map_int_double_to_py(<std_map[int, double]> c)
    return rtn


cdef class _Material(_Resource):

    @staticmethod
    def create(lib._Agent creator, double quantity, c, basis='mass'):
        """Creates a new material resource that is "live" and tracked. creator is a
        pointer to the agent creating the resource (usually will be the caller's
        "this" pointer). All future output data recorded will be done using the
        creator's context.
        """
        cdef shared_ptr[cpp_cyclus.Composition] comp = composition_ptr_from_py(c, basis)
        cdef _Material mat = Material(free=True)
        mat.ptx = <void*> cpp_cyclus.Material.Create(<cpp_cyclus.Agent*> creator.ptx,
                                                     quantity, comp)
        rtn = mat
        return mat

    @staticmethod
    def create_untracked(double quantity, c, basis='mass'):
        """Creates a new material resource that does not actually exist as part of
        the simulation and is untracked.
        """
        cdef shared_ptr[cpp_cyclus.Composition] comp = composition_ptr_from_py(c, basis)
        cdef _Material mat = Material(free=True)
        mat.ptx = <void*> cpp_cyclus.Material.CreateUntracked(quantity, comp)
        rtn = mat
        return mat

    def clone(self):
        """Returns an untracked (not part of the simulation) copy of the material.
        """
        cdef _Material co = Material(free=True)
        co._free = True
        co.ptx = <void*> (<cpp_cyclus.Material*> self.ptx).Clone()
        copy = co
        return copy

    def extract_qty(self, double quantity):
        """Same as ExtractComp with c = this->comp() and returns a Material,
        not a Resource.
        """
        cdef _Material res = Material(free=True)
        res.ptx = <void*> (<cpp_cyclus.Material*> self.ptx).ExtractQty(quantity)
        respy = res
        return respy

    def extract_comp(self, double qty, c, basis='mass', threshold=None):
        """Creates a new material by extracting from this one. """
        cdef shared_ptr[cpp_cyclus.Composition] comp = composition_ptr_from_py(c, basis)
        cdef double t
        t = cpp_cyclus.eps_rsrc() if threshold is None else threshold
        cdef _Material res = Material(free=True)
        res.ptx = <void*> (<cpp_cyclus.Material*> self.ptx).ExtractComp(qty, comp, t)
        respy = res
        return respy

    def absorb(self, _Material mat):
        """Combines material mat with this one.  mat's quantity becomes zero."""
        cdef shared_ptr[cpp_cyclus.Material] p = \
            shared_ptr[cpp_cyclus.Material](<cpp_cyclus.Material*> mat.ptx)
        (<cpp_cyclus.Material*> self.ptx).Absorb(p)

    def transmute(self, c, basis='mass'):
        """Changes the material's composition to c without changing its mass.  Use
        this method for things like converting fresh to spent fuel via burning in
        a reactor.
        """
        cdef shared_ptr[cpp_cyclus.Composition] comp = composition_ptr_from_py(c, basis)
        (<cpp_cyclus.Material*> self.ptx).Transmute(comp)

    def decay(self, int curr_time):
        """Updates the material's composition by performing a decay calculation.
        This is a special case of Transmute where the new composition is
        calculated automatically.  The time delta is calculated as the difference
        between curr_time and the last time the material's composition was
        updated with a decay calculation (i.e. prev_decay_time).  This may or may
        not result in an updated material composition.  Does nothing if the
        simulation decay mode is set to "never" or none of the nuclides' decay
        constants are significant with respect to the time delta.
        """
        (<cpp_cyclus.Material*> self.ptx).Decay(curr_time)

    @property
    def prev_decay_time(self):
        """The last time step on which a decay calculation was performed
        for the material.  This is not necessarily synonymous with the last time
        step the material's Decay function was called.
        """
        return (<cpp_cyclus.Material*> self.ptx).prev_decay_time()

    def decay_heat(self):
        """Returns a double with the decay heat of the material in units of W/kg."""
        return (<cpp_cyclus.Material*> self.ptx).DecayHeat()

    def comp(self, basis='mass'):
        """Returns the nuclide composition of this material."""
        rtn = composition_from_cpp((<cpp_cyclus.Material*> self.ptx).comp(), basis)
        return rtn


class Material(_Material, Resource):
    """The material class is primarily responsible for enabling basic material
    manipulation while helping enforce mass conservation.  It also provides the
    ability to easily decay a material up to the current simulation time; it
    does not perform any decay related logic itself.
    """


cdef class _Product(_Resource):
    pass


class Product(_Product, Resource):
    """yo"""



{% for t in ts.inventory_types %}{% set tclassname = ts.classname(t) %}
cdef class _{{tclassname}}:

    def __cinit__(self, init=False):
        self._free = init
        if init:
            self.ptx = new {{ts.cython_type(t)}}()
        else:
            self.ptx = NULL

    def __dealloc__(self):
        if self.ptx == NULL or not self._free:
            return
        del self.ptx


class {{tclassname}}(_{{tclassname}}):
    """An Inventory wrapper class for {{ts.norms[t]}}."""

{% endfor %}


#
# raw type definitions
#
{% for t in dbtypes %}
{{ t }} = {{ ts.cython_cpp_name(t) }}
{%- endfor %}

cdef dict C_RANKS = {
{%- for t in dbtypes %}
    {{ ts.cython_cpp_name(t) }}: {{ ts.ranks[t] }},
{%- endfor %}
    }
RANKS = C_RANKS

cdef dict C_NAMES = {
{%- for t in dbtypes %}
    {{ ts.cython_cpp_name(t) }}: '{{ t }}',
{%- endfor %}
    }
NAMES = C_NAMES

cdef dict C_IDS = {
{%- for t in dbtypes %}
    '{{ t }}': {{ ts.cython_cpp_name(t) }},
{%- endfor %}
{%- for t in ts.uniquetypes %}
    {{ repr(ts.norms[t]) }}: {{ ts.cython_cpp_name(t) }},
{%- endfor %}
    }
IDS = C_IDS

cdef dict C_CPPTYPES = {
{%- for t in dbtypes %}
    {{ ts.cython_cpp_name(t) }}: '{{ ts.cpptypes[t] }}',
{%- endfor %}
    }
CPPTYPES = C_CPPTYPES

cdef dict C_NORMS = {
{%- for t in dbtypes %}
    {{ ts.cython_cpp_name(t) }}: {{ repr(ts.norms[t]) }},
{%- endfor %}
    }
NORMS = C_NORMS

#
# converters
#
cdef bytes blob_to_bytes(cpp_cyclus.Blob value):
    rtn = value.str()
    return bytes(rtn)


cdef object uuid_cpp_to_py(cpp_cyclus.uuid x):
    cdef int i
    cdef list d = []
    for i in range(16):
        d.append(<unsigned int> x.data[i])
    rtn = uuid.UUID(hex=hexlify(bytearray(d)).decode())
    return rtn


cdef cpp_cyclus.uuid uuid_py_to_cpp(object x):
    cdef char * c
    cdef cpp_cyclus.uuid u
    if isinstance(x, uuid.UUID):
        c = x.bytes
    else:
        c = x
    memcpy(u.data, c, 16)
    return u

cdef std_string str_py_to_cpp(object x):
    cdef std_string s
    x = x.encode()
    s = std_string(<const char*> x)
    return s


{% for n in sorted(set(ts.norms.values()), key=ts.funcname) %}
{% set decl, body, expr = ts.convert_to_py('x', n) %}
cdef object {{ ts.funcname(n) }}_to_py({{ ts.cython_type(n) }} x):
    {{ decl | indent(4) }}
    {{ body | indent(4) }}
    return {{ expr }}
{%- endfor %}

{% for n in sorted(set(ts.norms.values()), key=ts.funcname) %}
{% set decl, body, expr = ts.convert_to_cpp('x', n) %}
cdef {{ ts.cython_type(n) }} {{ ts.funcname(n) }}_to_cpp(object x):
    {{ decl | indent(4) }}
    {{ body | indent(4) }}
    return {{ expr }}
{%- endfor %}


#
# type system functions
#
cdef object db_to_py(cpp_cyclus.hold_any value, cpp_cyclus.DbTypes dbtype):
    """Converts database types to python objects."""
    cdef object rtn
    {%- for i, t in enumerate(dbtypes) %}
    {% if i > 0 %}el{% endif %}if dbtype == {{ ts.cython_cpp_name(t) }}:
        rtn = {{ ts.hold_any_to_py('value', t) }}
    {%- endfor %}
    else:
        msg = "dbtype {0} could not be found while converting to Python"
        raise TypeError(msg.format(dbtype))
    return rtn


cdef cpp_cyclus.hold_any py_to_any(object value, object t):
    """Converts a Python object into int a hold_any instance by inspecting the
    type.

    Parameters
    ----------
    value : object
        A Python object to encapsulate.
    t : dbtype or norm type (str or tupe of str)
        The type to use in the conversion.
    """
    if isinstance(t, int):
        return py_to_any_by_dbtype(value, t)
    else:
        return py_to_any_by_norm(value, t)


cdef cpp_cyclus.hold_any py_to_any_by_dbtype(object value, cpp_cyclus.DbTypes dbtype):
    """Converts Python object to a hold_any instance by knowing the dbtype."""
    cdef cpp_cyclus.hold_any rtn
    {%- for i, t in enumerate(dbtypes) %}
    {% if i > 0 %}el{% endif %}if dbtype == {{ ts.cython_cpp_name(t) }}:
        rtn = {{ ts.py_to_any('rtn', 'value', t) }}
    {%- endfor %}
    else:
        msg = "dbtype {0} could not be found while converting from Python"
        raise TypeError(msg.format(dbtype))
    return rtn


cdef cpp_cyclus.hold_any py_to_any_by_norm(object value, object norm):
    """Converts Python object to a hold_any instance by knowing the dbtype."""
    cdef cpp_cyclus.hold_any rtn
    if isinstance(norm, str):
        {%- for i, t in enumerate(uniquestrtypes) %}
        {% if i > 0 %}el{% endif %}if norm == {{ repr(ts.norms[t]) }}:
            rtn = {{ ts.py_to_any('rtn', 'value', t) }}
        {%- endfor %}
        else:
            msg = "norm type {0} could not be found while converting from Python"
            raise TypeError(msg.format(norm))
    else:
        norm0 = norm[0]
        normrest = norm[1:]
        {% for i, (key, group) in enumerate(groupby(uniquetuptypes, key=firstfirst)) %}
        {% if i > 0 %}el{% endif %}if norm0 == {{ repr(key) }}:
            {%- for n, (tnorm, t) in enumerate(group) %}
            {% if n > 0 %}el{% endif %}if normrest == {{ repr(tnorm[1:]) }}:
                rtn = {{ ts.py_to_any('rtn', 'value', t) }}
            {%- endfor %}
            else:
                msg = "norm type {0} could not be found while converting from Python"
                raise TypeError(msg.format(norm))
        {% endfor %}
        else:
            msg = "norm type {0} could not be found while converting from Python"
            raise TypeError(msg.format(norm))
    return rtn


cdef object any_to_py(cpp_cyclus.hold_any value):
    """Converts any C++ object to its Python equivalent."""
    cdef object rtn = None
    cdef size_t valhash = value.type().hash_code()
    # Note that we need to use the *_t tyedefs here because of
    # Bug #1561 in Cython
    {%- for i, t in enumerate(ts.uniquetypes) %}
    {% if i > 0 %}el{% endif %}if valhash == typeid({{ ts.funcname(t) }}_t).hash_code():
        rtn = {{ ts.hold_any_to_py('value', t) }}
    {%- endfor %}
    else:
        msg = "C++ type could not be found while converting to Python"
        raise TypeError(msg)
    return rtn

#
# State Variable Descriptors
#

cdef class StateVar:
    """This class represents a state variable on a Cyclus agent.

    ============ ==============================================================
    key          meaning
    ============ ==============================================================
    type         The C++ type.  Valid types may be found on the :doc:`dbtypes`
                 page. **READ ONLY:** Do not set this key in
                 ``#pragma cyclus var {...}`` as it is set automatically by
                 cycpp. Feel free to use this downstream in your class or in a
                 post-process.
    index        Which number state variable is this, 0-indexed.
                 **READ ONLY:** Do not set this key in
                 ``#pragma cyclus var {...}`` as it is set automatically by
                 cycpp. Feel free to use this downstream in your class or in a
                 post-process.
    default      The default value for this variable that is used if otherwise
                 unspecified. The value must match the type of the variable.
    internal     ``True`` if this state variable is only for
                 archetype-internal usage.  Although the variable will still
                 be persisted in the database and initialized normally (e.g.
                 with any default), it will not be included in the XML schema
                 or input file.
    shape        The shape of a variable length datatypes. If present this must
                 be a list of integers whose length (rank) makes sense for this
                 type. Specifying positive values will (depending on the
                 backend) turn a variable length type into a fixed length one
                 with the length of the given value. Putting a ``-1`` in the
                 shape will retain the variable length nature along that axis.
                 Fixed length variables are normally more performant so it is
                 often a good idea to specify the shape where possible. For
                 example, a length-5 string would have a shape of ``[5]`` and
                 a length-10 vector of variable length strings would have a
                 shape of ``[10, -1]``.
    doc          Documentation string.
    tooltip      Brief documentation string for user interfaces.
    units        The physical units, if any.
    userlevel    Integer from 0 - 10 for representing ease (0) or difficulty (10)
                 in using this variable, default 0.
    alias        The name of the state variable in the schema and input file.
                 If this is not present it defaults to the C++ variable name.
                 The alias may also be a nested list of strings that matches
                 the C++ template type. Each member of the hierarchy will
                 recieve the corresponding alias.  For example, a
                 ``[std::map, int, double]`` could be aliased by
                 ``['recipe', 'id', 'mass']``. For maps, an additional item
                 tag is inserted. To also alias the item tag, make the top
                 alias into a 2-element list, whose first string represents
                 the map and whose second string is the item alias, e.g.
                 ``[['recipe', 'entry'], 'id', 'mass']``
    uilabel      The text string a UI will display as the name of this input on
                 the UI input form.
    uitype       The type of the input field in reference in a UI,
                 currently supported types are; incommodity, outcommodity,
                 commodity, range, combobox, facility, prototype, recipe, nuclide,
                 and none.
                 For 'nuclide' when the type is an int, the values will be read in
                 from the input file in human readable string format ('U-235') and
                 automatically converted to results of ``pyne::nucname::id()``
                 (922350000) in the database and on the archetype.
    range        This indicates the range associated with a range type.
                 It must take the form of ``[min, max]`` or
                 ``[min, max, (optional) step size]``.
    categorical  This indicates the decrete values a combobox Type can take. It
                 must take the form of ``[value1, value2, value3, etc]``.
    schematype   This is the data type that is used in the schema for input file
                 validation. This enables you to supply just the data type
                 rather than having to overwrite the full schema for this state
                 variable. In most cases - when the shape is rank 0 or 1 such
                 as for scalars or vectors - this is simply a string. In cases
                 where the rank is 2+ this is a list of strings. Please refer to
                 the `XML Schema Datatypes <http://www.w3.org/TR/xmlschema-2/>`_
                 page for more information. *New in v1.1.*
    initfromcopy Code snippet to use in the ``InitFrom(Agent* m)`` function for
                 this state variable instead of using code generation.
                 This is a string.
    initfromdb   Code snippet to use in the ``InitFrom(QueryableBackend* b)``
                 function for this state variable instead of using code generation.
                 This is a string.
    infiletodb   Code snippets to use in the ``InfileToDb()`` function
                 for this state variable instead of using code generation.
                 This is a dictionary of string values with the two keys 'read'
                 and 'write' which represent reading values from the input file
                 writing them out to the database respectively.
    schema       Code snippet to use in the ``schema()`` function for
                 this state variable instead of using code generation.
                 This is an RNG string. If you supply this then you likely need
                 to supply ``infiletodb`` as well to ensure that your custom
                 schema is read into the database correctly.
    snapshot     Code snippet to use in the ``Snapshot()`` function for
                 this state variable instead of using code generation.
                 This is a string.
    snapshotinv  Code snippet to use in the ``SnapshotInv()`` function for
                 this state variable instead of using code generation.
                 This is a string.
    initinv      Code snippet to use in the ``InitInv()`` function for
                 this state variable instead of using code generation.
                 This is a string.
    uniquetypeid The dbtype id for the type that is unique among all dbtypes
                 for a given C++ representations. **READ ONLY:** Do not set this
                 key!!!
    ============ ==============================================================
    """


    def __cinit__(self, object value=None,
            {%- for pyname, cppname, typename in annotations -%}
            {{typename}} {{pyname}}=None,
            {%- endfor -%}):
        self.value = value
        {% for pyname, cppname, _ in annotations -%}
        self.{{cppname}} = {{pyname}}
        {% endfor %}

    {% for pyname, cppname, typename in annotations -%}{% if pyname != cppname %}
    @property
    def {{pyname}}(self):
        return self.{{cppname}}

    @{{pyname}}.setter
    def {{pyname}}(self, {{typename}} value):
        self.{{cppname}} = value
    {% endif %}{% endfor %}

    #
    # Descriptor interface
    #
    def __get__(self, obj, cls):
        return self.value

    def __set__(self, obj, val):
        self.value = val

    cpdef dict to_dict(self):
        """Returns a representation of this state variable as a dict."""
        return {'value': self.value,
            {%- for pyname, cppname, _ in annotations -%}
            '{{pyname}}': self.{{cppname}},
            {%- endfor -%}
            }

    cpdef StateVar copy(self):
        """Copies the state variable into a new instance."""
        return StateVar(value=self.value,
            {%- for pyname, cppname, _ in annotations -%}
            {{pyname}}=self.{{cppname}},
            {%- endfor -%}
            )

{% for t in ts.uniquetypes %}{% if t not in ts.inventory_types %}{% set tclassname = ts.classname(t) %}
cdef class {{tclassname}}(StateVar):
    """State variable descriptor for {{ts.cpptypes[t]}}"""

    def __cinit__(self, object value=None,
            {%- for pyname, cppname, typename in annotations -%}{%- if pyname not in nonuser_annotations -%}
            {{typename}} {{pyname}}=None,
            {%- endif -%}{%- endfor -%}):
        self.value = value
        {% for pyname, cppname, _ in annotations -%}
        {% if pyname == 'type' %}
        self.type = {{repr(ts.norms[t])}}
        {% elif pyname == 'uniquetypeid' %}
        self.uniquetypeid = {{ts.ids[t]}}
        {%- else %}
        self.{{cppname}} = {{pyname}}
        {%- endif -%}{% endfor %}

    cpdef {{tclassname}} copy(self):
        """Copies the {{tclassname}} into a new instance."""
        return {{tclassname}}(value=self.value,
            {%- for pyname, cppname, _ in annotations -%}{%- if pyname not in nonuser_annotations -%}
            {{pyname}}=self.{{cppname}},
            {%- endif -%}{%- endfor -%}
            )

{% endif %}{% endfor %}

#
# Helpers
#
def prepare_type_representation(cpptype, othertype):
    """Updates othertype to conform to the length of cpptype using None's.
    """
    cdef int i, n
    if not isinstance(cpptype, str):
        n = len(cpptype)
        if isinstance(othertype, str):
            othertype = [othertype]
        if othertype is None:
            othertype = [None] * n
        elif len(othertype) < n:
            othertype.extend([None] * (n - len(othertype)))
        # recurse down
        for i in range(1, n):
            othertype[i] = prepare_type_representation(cpptype[i], othertype[i])
        return othertype
    else:
        return othertype


'''.lstrip())


def typesystem_pyx(ts, ns):
    """Creates the Cython wrapper for the Cyclus type system."""
    nonuser_annotations = ('type', 'uniquetypeid')
    ctx = dict(
        ts=ts,
        dbtypes=ts.dbtypes,
        cg_warning=CG_WARNING,
        npy_imports=NPY_IMPORTS,
        stl_cimports=STL_CIMPORTS,
        set=set,
        repr=repr,
        sorted=sorted,
        enumerate=enumerate,
        annotations=ANNOTATIONS,
        nonuser_annotations=nonuser_annotations,
        uniquestrtypes = [t for t in ts.uniquetypes
                          if isinstance(ts.norms[t], str)],
        uniquetuptypes = sorted([(ts.norms[t], t) for t in ts.uniquetypes
                                 if not isinstance(ts.norms[t], str)], reverse=True,
                                key=lambda x: (x[0][0], x[1])),
        groupby=itertools.groupby,
        firstfirst=lambda x: x[0][0],
        )
    rtn = TYPESYSTEM_PYX.render(ctx)
    return rtn

TYPESYSTEM_PXD = JENV.from_string('''
{{ cg_warning }}

{{ stl_cimports }}

# local imports
from cyclus cimport cpp_typesystem
from cyclus cimport cpp_cyclus

#
# Resources & Inventories
#

cdef class _Resource:
    cdef void * ptx
    cdef bint _free

cdef shared_ptr[cpp_cyclus.Composition] composition_ptr_from_py(object, object)
cdef object composition_from_cpp(shared_ptr[cpp_cyclus.Composition] comp, object basis)

cdef class _Material(_Resource):
    pass

cdef class _Product(_Resource):
    pass

{% for t in ts.inventory_types %}
{% set tclassname = ts.classname(t)%}
cdef class _{{tclassname}}:
    cdef {{ts.cython_type(t)}}* ptx
    cdef bint _free
{% endfor %}

#
# raw
#
cpdef dict C_RANKS
cpdef dict C_NAMES
cpdef dict C_IDS
cpdef dict C_CPPTYPES
cpdef dict C_NORMS

#
# typedefs
#
{% for t in ts.uniquetypes %}
ctypedef {{ ts.cython_type(t) }} {{ ts.funcname(t) }}_t
{%- endfor %}

#
# converters
#
cdef bytes blob_to_bytes(cpp_cyclus.Blob value)

cdef object uuid_cpp_to_py(cpp_cyclus.uuid x)


cdef cpp_cyclus.uuid uuid_py_to_cpp(object x)

cdef std_string str_py_to_cpp(object x)

{% for n in sorted(set(ts.norms.values()), key=ts.funcname) %}
cdef object {{ ts.funcname(n) }}_to_py({{ ts.cython_type(n) }} x)
{%- endfor %}

{% for n in sorted(set(ts.norms.values()), key=ts.funcname) %}
cdef {{ ts.cython_type(n) }} {{ ts.funcname(n) }}_to_cpp(object x)
{%- endfor %}


#
# type system functions
#
cdef object db_to_py(cpp_cyclus.hold_any value, cpp_cyclus.DbTypes dbtype)

cdef cpp_cyclus.hold_any py_to_any(object value, object t)

cdef cpp_cyclus.hold_any py_to_any_by_dbtype(object value, cpp_cyclus.DbTypes dbtype)

cdef cpp_cyclus.hold_any py_to_any_by_norm(object value, object norm)

cdef object any_to_py(cpp_cyclus.hold_any value)

#
# State Variable Descriptors
#

cdef class StateVar:
    cdef public object value
    {% for pyname, cppname, typename in annotations %}
    cdef public {{typename}} {{cppname}}
    {%- endfor %}
    cpdef dict to_dict(self)
    cpdef StateVar copy(self)

{% for t in ts.uniquetypes %}{% if t not in ts.inventory_types %}{% set tclassname = ts.classname(t) %}
cdef class {{tclassname}}(StateVar):
    cpdef {{tclassname}} copy(self)
{% endif %}{% endfor %}

''')

def typesystem_pxd(ts, ns):
    """Creates the Cython wrapper header for the Cyclus type system."""
    ctx = dict(
        ts=ts,
        dbtypes=ts.dbtypes,
        cg_warning=CG_WARNING,
        npy_imports=NPY_IMPORTS,
        stl_cimports=STL_CIMPORTS,
        set=set,
        sorted=sorted,
        enumerate=enumerate,
        annotations=ANNOTATIONS,
        )
    rtn = TYPESYSTEM_PXD.render(ctx)
    return rtn


#
# CLI
#

def parse_args(argv):
    """Parses typesystem arguments for code generation."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose', default=False, action='store_true',
                        dest='verbose',
                        help="whether to give extra information at run time.")
    parser.add_argument('--src-dir', default='.', dest='src_dir',
                        help="the local source directory, default '.'")
    parser.add_argument('--test-dir', default='tests', dest='test_dir',
                        help="the local tests directory, default 'tests'")
    parser.add_argument('--build-dir', default='build', dest='build_dir',
                        help="the local build directory, default 'build'")
    parser.add_argument('--cpp-typesystem', default='cpp_typesystem.pxd',
                        dest='cpp_typesystem',
                        help="the name of the C++ typesystem header, "
                             "default 'cpp_typesystem.pxd'")
    parser.add_argument('--typesystem-pyx', default='typesystem.pyx',
                        dest='typesystem_pyx',
                        help="the name of the Cython typesystem wrapper, "
                             "default 'typesystem.pyx'")
    parser.add_argument('--typesystem-pxd', default='typesystem.pxd',
                        dest='typesystem_pxd',
                        help="the name of the Cython typesystem wrapper header, "
                             "default 'typesystem.pxd'")
    dbtd = os.path.join(os.path.dirname(__file__), '..', 'share', 'dbtypes.json')
    parser.add_argument('--dbtypes-json', default=dbtd,
                        dest='dbtypes_json',
                        help="the path to dbtypes.json file, "
                             "default " + dbtd)
    parser.add_argument('--cyclus-version', default=None,
                        dest='cyclus_version',
                        help="The Cyclus API version to target."
                        )
    ns = parser.parse_args(argv)
    return ns


def setup(ns):
    """Ensure that we are ready to perform code generation. Returns typesystem."""
    if not os.path.exists(ns.build_dir):
        os.mkdir(ns.build_dir)
    if not os.path.isfile(ns.dbtypes_json):
        try:
            instdir = safe_output(['cyclus', '--install-path'])
        except (subprocess.CalledProcessError, OSError):
            # fallback for conda version of cyclus
            instdir = safe_output(['cyclus_base', '--install-path'])
        ns.dbtypes_json = os.path.join(instdir.strip().decode(), 'share',
                                       'cyclus', 'dbtypes.json')
    with io.open(ns.dbtypes_json, 'r') as f:
        tab = json.load(f)
    # get cyclus version
    verstr = ns.cyclus_version
    if verstr is None:
        try:
            verstr = safe_output(['cyclus', '--version']).split()[2]
        except (subprocess.CalledProcessError, OSError):
            # fallback for conda version of cyclus
            try:
                verstr = safe_output(['cyclus_base', '--version']).split()[2]
            except (subprocess.CalledProcessError, OSError):
                # fallback using the most recent value in JSON
                ver = set([row[5] for row in tab[1:]])
                ver = max([tuple(map(int, s[1:].partition('-')[0].split('.'))) for s in ver])
    if verstr is not None:
        if isinstance(verstr, bytes):
            verstr = verstr.decode()
        ns.cyclus_version = verstr
        ver = tuple(map(int, verstr.partition('-')[0].split('.')))
    if ns.verbose:
        print('Found cyclus version: ' + verstr, file=sys.stderr)
    # make and return a type system
    ts = TypeSystem(table=tab, cycver=ver, rawver=verstr,
            cpp_typesystem=os.path.splitext(ns.cpp_typesystem)[0])
    return ts


def code_gen(ts, ns):
    """Generates code given a type system and a namespace."""
    cases = [(cpp_typesystem, ns.cpp_typesystem),
             (typesystem_pyx, ns.typesystem_pyx),
             (typesystem_pxd, ns.typesystem_pxd),]
    for func, basename in cases:
        s = func(ts, ns)
        fname = os.path.join(ns.src_dir, basename)
        orig = None
        if os.path.isfile(fname):
            with io.open(fname, 'r') as f:
                orig = f.read()
        if orig is None or orig != s:
            with io.open(fname, 'w') as f:
                f.write(s)


def main(argv=None):
    """Entry point into the code generation. Accepts list of command line arguments."""
    if argv is None:
        argv = sys.argv[1:]
    ns = parse_args(argv)
    ts = setup(ns)
    code_gen(ts, ns)


if __name__ == "__main__":
    main()
