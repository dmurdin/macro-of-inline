from pycparser import c_ast

import enum
import os
import cfg
import ext_pycparser
import pycparser
import recorder
import utils
import re

def cpp(filename):
	"""
	File -> Text
	"""
	# TODO Use pkg_resources or something that fits more.
	p = os.path.join(os.path.dirname(__file__), 'fake_libc_include')
	cpp_args = ['-U__GNUC__', r'-I%s' % p]

	cpp_args.extend([r'%s' % utils.to_option(option) for option in cfg.t.extra_options])

	# The raw output of mcpp can contain _Pragma() lines that can't be parsed by pycparser.
	# Now we remove these lines however, can't suppose this adhoc patch will work for any cases.
	output = utils.preprocess_file(filename, cpp_path='mcpp', cpp_args=cpp_args)
	return '\n'.join([x for x in output.split('\n') if not x.startswith("_Pragma(")])

def analyzeInclude(filename, txt, orig_txt):      
	"""
	Text -> (include_directives, Text)
	"""
        current_result = None
        include_statements = []
        include_code = []
        lineno = 0
        orig_txt_lines = orig_txt.splitlines()
    
        for line in txt.splitlines():
            if line.startswith("#line"):
                xs = line.split()
                fn = xs[2].strip('"')
                fn = fn.replace('\\\\', '\\')
                if fn == filename:
                    lineno = int(xs[1])
                    current_result = None
                elif current_result == None:
                    current_result = []
                    # search for matching include statement
                    for inc_no in range(lineno-1, len(orig_txt_lines)):
                        if re.match('#include.*' + os.path.basename(fn), orig_txt_lines[inc_no]):
                            include_statements.append(orig_txt_lines[inc_no])
                    include_code.append(current_result)
            else:
                if current_result == None:
                    lineno += 1
                else:
                    current_result.append(line)
        return (include_statements, include_code)

def compare_asts(ast1, ast2):
	if type(ast1) != type(ast2):
		return False

	# In the function block, calls of inline function may be expanded.
	# We shouldn't depend on the way they are expanded but only on the function name.
	# At least, the function call is added a new argument "namespace" then
	# we can't use pure equality of ASTs here.
	if isinstance(ast1, c_ast.FuncDef) and isinstance(ast2, c_ast.FuncDef):
		return ast1.decl.name == ast2.decl.name

	if isinstance(ast1, tuple) and isinstance(ast2, tuple):
		if ast1[0] != ast2[0]:
			return False
		ast1 = ast1[1]
		ast2 = ast2[1]
		return compare_asts(ast1, ast2)

	for attr in ast1.attr_names:
		if getattr(ast1, attr) != getattr(ast2, attr):
			return False

	if len(ast1.children()) != len(ast2.children()):
		return False

	for c1, c2 in zip(ast1.children(), ast2.children()):
		if compare_asts(c1, c2) == False:
			return False

	return True

class ASTDiff:
	def __init__(self):
		self.asts = [] # [[ast, count]]

	def inc(self, ast):
		for e in self.asts:
			if compare_asts(e[0], ast):
				e[1] += 1
				return
		self.asts.append([ast, 1])

	def dec(self, ast):
		"""
		Return true iff the ast exists (count > 0)
		"""
		for e in self.asts:
			if compare_asts(e[0], ast):
				if (e[1] > 0):
					e[1] -= 1
					return True
				else:
					return False
		return False

def ast_delete(a, b):
	"""
	AST-level deletion
	a -= b 

	Assumes that header directives are listed
	at the head of the target file.
	"""
	diff = ASTDiff()

	for n in b.ext:
		diff.inc(n)

	delete_indices = []
	for i, n in enumerate(a.ext):
		if diff.dec(n):
			delete_indices.append(i)

	for i in reversed(delete_indices):
		del(a.ext[i])

class Apply:
	"""
	(AST -> AST) -> File -> Text

	The file (of filename) can be before preprocessing.
	It can contains directives.
	"""
	def __init__(self, f):
		self.f = f

	def on(self, filename):
		cpped_txt = cpp(filename)
		recorder.t.file_record("preprocessed", cpped_txt)
		# print(cpped_txt)

                fp = open(filename)
                orig_txt = fp.read()
                fp.close()

		(included_headers, included_code) = analyzeInclude(filename, cpped_txt, orig_txt)
                included_codes=[];
                for inc_code in included_code:
                        included_codes('\n'.join(inc_code))

		# print(includes)


		ast_a = self.f(cpped_txt)

		ast_b = ext_pycparser.ast_of('\n'.join(included_codes))
		ast_delete(ast_a, ast_b)
		recorder.t.file_record("delete_included_decls", ext_pycparser.CGenerator().visit(ast_a))

		contents = ext_pycparser.CGenerator().visit(ast_a)

		contents =  """
%s
%s

""" % ('\n'.join(included_headers), ext_pycparser.CGenerator.cleanUp(contents))

		recorder.t.file_record("union_header_directives", contents)
		return contents

if __name__ == "__main__":
	testcase = r"""
#line 1 "main.c"
#line 1 "<command-line>"
#line 1 "main.c"
#line 1 "/usr/lib/gcc/x86_64-linux-gnu/4.7/include/stdarg.h" 1 3 4
#line 40 "/usr/lib/gcc/x86_64-linux-gnu/4.7/include/stdarg.h" 3 4
typedef __builtin_va_list __gnuc_va_list;
#line 102 "/usr/lib/gcc/x86_64-linux-gnu/4.7/include/stdarg.h" 3 4
typedef __gnuc_va_list va_list;
#line 2 "main.c" 2
#line 1 "a.h" 1

#line 1 "f/g.h" 1
typedef long mylong;
#line 3 "a.h" 2
struct T { int x; };

struct U {
 int y;
  int z;
};
#line 3 "main.c" 2
#line 1 "b.h" 1
#line 1 "c.h" 1
#line 2 "b.h" 2
#line 1 "d.h" 1
#line 1 "e.h" 1
#line 1 "d.h" 2
#line 3 "b.h" 2
#line 1 "f/g.h" 1
typedef long mylong;
#line 3 "b.h" 2
#line 4 "main.c" 2


int main(void) { return 0; }
"""	
	analyzeInclude("main.c", testcase, testcase)

	a = r"""
int x1;
int x2;
struct T1 { int x; };
struct T2 { int x; };
typedef int int1;
typedef int int2;
struct f;
void f1();
void f1() {}
void f2() {}

int main()
{
	return 0;
}
"""
	ast_a = ext_pycparser.ast_of(a)

	b = r"""
int x1;
struct T1 { int x; };
typedef int int1;
void f1() {}
void f2();
typedef struct T3 { int x; } t3;
typedef int int1;
"""
	ast_b = ext_pycparser.ast_of(b)

	ast_delete(ast_a, ast_b)
