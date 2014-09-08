from pycparser import c_ast, c_generator

import recorder
import cfg
import copy
import inspect
import rewrite_fun
import pycparser_ext

class VoidFun(rewrite_fun.Fun):
	"""
	Rewrite function definitions
	1. Return type T to void: T f(..) -> void f(..)
	2. New argument for returning value: f(..) -> f(.., T *retval)
	3. Return statement to assignment: return x -> *retval = x
	"""

	PHASES = [
		"add_ret_val",
		"void_return_type",
		"return_to_assignment",
	]

	def __init__(self, func):
		self.func = func
		self.phase_no = 0

	def addRetval(self):
		funtype = self.func.decl.type
		rettype = copy.deepcopy(funtype.type)
		rewrite_fun.RewriteTypeDecl("retval").visit(rettype)
		newarg = c_ast.Decl("retval", [], [], [], c_ast.PtrDecl([], rettype), None, None)
		params = []
		if not self.voidArgs():
			params = funtype.args.params
		params.insert(0, newarg)
		if not funtype.args:
			funtype.args = c_ast.ParamList([])
		funtype.args.params = params
		return self

	def voidReturnValue(self):
		self.phase_no += 1
		self.func.decl.type.type = c_ast.TypeDecl(self.name(), [], c_ast.IdentifierType(["void"]))
		return self

	class ReturnToAssignment(pycparser_ext.NodeVisitor):
		def visit_Return(self, n):
			pycparser_ext.NodeVisitor.rewrite(self.current_parent, self.current_name,
				c_ast.Assignment("=",
						c_ast.UnaryOp("*", c_ast.ID("retval")), # lvalue
						n.expr)) # rvalue

			assert(isinstance(self.current_parent, c_ast.Compound))
			# Since we are visiting in depth-first and
			# pycparser's children() method first create nodelist
			# it is safe to add some node as sibling (but it won't be visited)
			self.current_parent.block_items.append(c_ast.Return(None))

	def rewriteReturn(self):
		self.phase_no += 1
		self.ReturnToAssignment().visit(self.func)
		return self

	def run(self):		
		return self.addRetval().show().voidReturnValue().show().rewriteReturn().show()

	def returnAST(self):
		return self.func

	def show(self):
		recorder.fun_record(self.PHASES[self.phase_no], self.func)
		return self


class FuncCallName(c_ast.NodeVisitor):
	"""
	Usage: visit(FuncCall.name)

	A call might be of form "(*f)(...)"
	The AST is then

	FuncCall
	  UnaryOp
		ID

	While ordinary call is of

	FuncCall
	  ID

	We need to find the ID node recursively.

	Note:
	The AST representation of f->f(hoge) is like this.
	As a result, this function returns 'f' as the name of this call.
	This works because 'f' shadows the non-void functions.
	(We don't expect f->f(hoge) will be written to f->f(&retval, hoge))

        FuncCall:
          StructRef: ->
            ID: f
            ID: f
          ExprList:
            ID: hoge
	"""
	def __init__(self):
		self.found = False

	def visit_ID(self, n):
		if self.found: return
		self.result = n.name
		self.found = True

class SymbolTable:
	def __init__(self):
		self.names = set()
		self.prev_table = None

	def register(self, name):
		self.names.add(name)

	def clone(self):
		st = SymbolTable()
		st.names = copy.deepcopy(self.names)
		return st

	def show(self):
		print(self.names)

class RewriteFun:
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

	class RewriteToCommaOp(pycparser_ext.NodeVisitor):
		def __init__(self, context):
			self.cur_table = SymbolTable()
			self.context = context

			if not self.context.func.decl.type.args:
				return

			# Because recursive function will not be macroized
			# we don't need care shadowing by it's own function name.
			for param_decl in self.context.func.decl.type.args.params or []:
				if isinstance(param_decl, c_ast.EllipsisParam): # ... (EllipsisParam)
					continue
				self.cur_table.register(param_decl.name)

		def switchTable(self):
			new_table = self.cur_table.clone()
			new_table.prev_table = self.cur_table
			self.cur_table = new_table

		def revertTable(self):
			self.cur_table = self.cur_table.prev_table;

		def visit_Compound(self, n):
			self.switchTable()
			pycparser_ext.NodeVisitor.generic_visit(self, n)
			self.revertTable()

		def mkCommaOp(self, var, f):
			"""
			var = f()

			=>

			(f(&var), var)
			"""
			proc = f
			if not proc.args:
				proc.args = c_ast.ExprList([])
			proc.args.exprs.insert(0, c_ast.UnaryOp("&", var))
			return c_ast.ExprList([proc, var])

		def visit_FuncCall(self, n):
			funcname = FuncCallName()
			funcname.visit(n)
			funcname = funcname.result

			unshadowed_names = self.context.non_void_names - self.cur_table.names
			if not funcname in unshadowed_names:
				pycparser_ext.NodeVisitor.generic_visit(self, n)
				return

			if (isinstance(self.current_parent, c_ast.Assignment)):
				comma = self.mkCommaOp(self.current_parent.lvalue, n)
			else:
				randvar = rewrite_fun.newrandstr(cfg.env.rand_names, rewrite_fun.N)

				# Generate "T var" from the function definition "T f(...)"
				func = (m for _, m in self.context.non_void_funs if rewrite_fun.Fun(m).name() == funcname).next()
				old_decl = copy.deepcopy(func.decl.type.type)
				rewrite_fun.RewriteTypeDecl(randvar).visit(old_decl)
				self.context.func.body.block_items.insert(0, c_ast.Decl(randvar, [], [], [], old_decl, None, None))

				comma = self.mkCommaOp(c_ast.ID(randvar), n)

			pycparser_ext.NodeVisitor.rewrite(self.current_parent, self.current_name, comma)
			pycparser_ext.NodeVisitor.generic_visit(self, n)

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
		recorder.fun_record(self.PHASES[self.phase_no], self.func)
		return self

class RewriteFile:
	"""
	AST -> AST
	"""
	def __init__(self, ast):
		self.ast = ast
		self.non_void_funs = []

	def run(self):
		for i, n in enumerate(self.ast.ext):
			if not isinstance(n, c_ast.FuncDef):
				continue
			
			if not rewrite_fun.Fun(n).doMacroize():
				continue

			if not rewrite_fun.Fun(n).returnVoid():
				self.non_void_funs.append((i, n))

		old_non_void_funs = copy.deepcopy(self.non_void_funs)

		# Rewrite definitions
		for i, n in self.non_void_funs:
			self.ast.ext[i] = VoidFun(n).run().returnAST()
		recorder.file_record("rewrite_func_defines", c_generator.CGenerator().visit(self.ast))

		# Rewrite all callers
		for i, n in enumerate(self.ast.ext):
			if not isinstance(n, c_ast.FuncDef):
				continue
			self.ast.ext[i] = RewriteFun(n, old_non_void_funs).run().returnAST()
		recorder.file_record("rewrite_all_callers", c_generator.CGenerator().visit(self.ast))

		return self

	def returnAST(self):
		return self.ast

test_fun = r"""
inline struct T *f(int n)
{
	struct T *t = init_t();
	t->x = n;
	return g(t);
}
"""

test_fun2 = r"""
inline int f(int x, ...) {
	if (1) {
		return 1;
	} else {
		return 0;
	}
} 
"""

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
	do {
		int hR = h1(h1(h2(h3(0))));
	} while(0);
	int p;
	int q = 3;
	int hRR = t->h1(h1(h2(h3(0))));
	return g(x, f());
}

int bar() {}
"""

if __name__ == "__main__":
	ast = pycparser_ext.ast_of(test_file)
	ast.show()
	ast = RewriteFile(ast).run().returnAST()
	ast.show()
	print c_generator.CGenerator().visit(ast)

	# fun = pycparser_ext.ast_of(test_fun2).ext[0]
	# fun.show()
	# ast = VoidFun(fun).run().returnAST()
	# print c_generator.CGenerator().visit(ast)
