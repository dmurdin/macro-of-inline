from pycparser import c_ast, c_generator

import cfg
import ext_pycparser
import inspect
import recorder
import rewrite
import rewrite_void_fun
import rewrite_non_void_fun
import utils

class SymbolTable:
	def __init__(self, func):
		self.names = set()
		self.prev_table = None

	def register(self, name):
		self.names.add(name)

	def register_args(self, func):
		if ext_pycparser.FuncDef(func).voidArgs():
			return

		# Because recursive function will not be macroized
		# we don't need care shadowing by it's own function name.
		for param_decl in func.decl.type.args.params or []:
			if isinstance(param_decl, c_ast.EllipsisParam): # ... (Ellipsisparam)
				continue
			self.register(param_decl.name)

	def clone(self):
		st = SymbolTable()
		st.names = copy.deepcopy(self.names)
		return st

	def switch(self):
		new_table = self.clone()
		new_table.prev_table = self.cur_table
		return new_table

	def show(self):
		print(self.names)

class RewriteCaller:
	"""
	Rewrite all functions
	that may call the rewritten (non-void -> void) functions.
	"""
	PHASES = [
		"split_decls",
		"pop_fun_calls",
		"rewrite_calls",
	]

	def __init__(self, func, non_void_funs):
		self.func = func
		self.phase_no = 0
		self.non_void_funs = non_void_funs
		self.non_void_names = set([rewrite_fun.Fun(n).name() for _, n in self.non_void_funs])

	class DeclSplit(c_ast.NodeVisitor):
		"""
		int x = v;

		=>

		int x; (lining this part at the beginning of the function block)
		x = v;
		"""
		def visit_Compound(self, n):
			decls = []
			for i, item in enumerate(n.block_items or []):
				if isinstance(item, c_ast.Decl):
					decls.append((i, item))

			for i, decl in reversed(decls):
				if decl.init:
					n.block_items[i] = c_ast.Assignment("=",
							c_ast.ID(decl.name), # lvalue
							decl.init) # rvalue
				else:
					del n.block_items[i]

			for _, decl in reversed(decls):
				decl_var = copy.deepcopy(decl)
				# TODO Don't split int x = <not func>.
				# E.g. int r = 0;
				decl_var.init = None
				n.block_items.insert(0, decl_var)

			c_ast.NodeVisitor.generic_visit(self, n)

	class RewriteToCommaOp(ext_pycparser.NodeVisitor):
		def __init__(self, context):
			self.cur_table = SymbolTable()
			self.context = context
			self.cur_table.register_args(self.context.func)

		def switchTable(self):
			self.cur_table = self.cur_table.switch()

		def revertTable(self):
			self.cur_table = self.cur_table.prev_table;

		def visit_Compound(self, n):
			self.cur_table = self.cur_table.switch()
			ext_pycparser.NodeVisitor.generic_visit(self, n)
			self.cur_table = self.cur_table.prev_table;

		def mkCommaOp(self, var, f):
			proc = f
			if not proc.args:
				proc.args = c_ast.ExprList([])
			proc.args.exprs.insert(0, c_ast.UnaryOp("&", var))
			return ext_pycparser.CommaOp(c_ast.ExprList([proc, var]))

		def visit_FuncCall(self, n):
			"""
			var = f() => var = (f(&var), var)
			f()       => (f(&randvar), randvar)
			"""
			funcname = ext_pycparser.FuncCallName()
			funcname.visit(n)
			funcname = funcname.result

			unshadowed_names = self.context.non_void_names - self.cur_table.names
			if funcname in unshadowed_names:

				if (isinstance(self.current_parent, c_ast.Assignment)):
					comma = self.mkCommaOp(self.current_parent.lvalue, n)
				else:
					randvar = rewrite_fun.newrandstr(rewrite.t.rand_names, rewrite_fun.N)

					# Generate "T var" from the function definition "T f(...)"
					func = (m for _, m in self.context.non_void_funs if rewrite_fun.Fun(m).name() == funcname).next()
					old_decl = copy.deepcopy(func.decl.type.type)
					rewrite_fun.RewriteTypeDecl(randvar).visit(old_decl)
					self.context.func.body.block_items.insert(0, c_ast.Decl(randvar, [], [], [], old_decl, None, None))

					comma = self.mkCommaOp(c_ast.ID(randvar), n)

				ext_pycparser.NodeVisitor.rewrite(self.current_parent, self.current_name, comma)

			ext_pycparser.NodeVisitor.generic_visit(self, n)

	def run(self):
		self.DeclSplit().visit(self.func)
		self.show()

		self.phase_no += 1
		self.RewriteToCommaOp(self).visit(self.func)
		self.show()

		return self

	def returnAST(self):
		return self.func

	def show(self):
		recorder.t.fun_record(self.PHASES[self.phase_no], self.func)
		return self

class Main:
	"""
	AST -> AST
	"""
	def __init__(self, ast):
		self.ast = ast
		self.non_void_funs = []

	def run(self):

		old_non_void_funs = copy.deepcopy(self.non_void_funs)

		# Rewrite definitions
		for i, n in self.non_void_funs:
			self.ast.ext[i] = rewrite_non_void_fun.Main(n).run().returnAST()
		recorder.t.file_record("rewrite_func_defines", c_generator.CGenerator().visit(self.ast))

		# Rewrite all callers
		for i, n in enumerate(self.ast.ext):
			if not isinstance(n, c_ast.FuncDef):
				continue
			self.ast.ext[i] = RewriteCaller(n, old_non_void_funs).run().returnAST()
		recorder.t.file_record("rewrite_all_callers", c_generator.CGenerator().visit(self.ast))

		return self

	def returnAST(self):
		return self.ast

test_file = r"""
inline int f(void) { return 0; }
inline int g(int a, int b) { return a * b; }

inline int h1(int x) { return x; }
int h2(int x) { return x; }
inline int h3(int x) { return x; }

void r(int x) {}

int foo(int x, ...)
{
	int x = f();
	r(f());
	x += 1;
	int y = g(z, g(y, (*f)()));
	int z = 2;
	int hR = h1(h1(h2(h3(0))));
	if (0)
		return h1(h1(0));
	do {
		int hR = h1(h1(h2(h3(0))));
		if (0)
			return h1(h1(0));
	} while(0);
	int p;
	int q = 3;
	int hRR = t->h1(h1(h2(h3(0))));
	return g(x, f());
}

int bar() {}
"""

if __name__ == "__main__":
	ast = ext_pycparser.ast_of(test_file)
	ast.show()
	ast = Main(ast).run().returnAST()
	ast.show()
	print ext_pycparser.CGenerator().visit(ast)
