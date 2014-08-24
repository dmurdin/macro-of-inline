from pycparser import c_parser, c_ast

import pycparser_ext
import collections
import string
import random
import copy
import enum

Symbol = collections.namedtuple('Symbol', 'alias, overwritable')

DEBUG=False
def P(s):
	if not DEBUG:
		return
	print(s)

def randstr(n):
	return ''.join(random.choice(string.letters) for i in xrange(n))

N = 16
class NameTable:
	def __init__(self):
		self.table = {}
		self.prev_table = None

	def register(self, name):
		alias = randstr(N)
		self.table[name] = Symbol(alias, overwritable=False)

	def declare(self, name):
		if name in self.table:
			if self.table[name].overwritable:
				self.register(name)
		else:
			self.register(name)

	def alias(self, name):
		if name in self.table:
			return self.table[name].alias
		else:
			return name	

	def clone(self):
		new = {}
		for name in self.table:
			new[name] = Symbol(self.table[name].alias, overwritable=True)
		nt = NameTable()
		nt.table = new
		return nt
	
	def show(self):
		if not DEBUG:
			return
		print("NameTable")
		for name in self.table:
			tup = self.table[name]
			print("  %s -> (alias:%s, overwritable:%r)" % (name, tup.alias, tup.overwritable))

ArgType = enum.Enum("ArgType", "other fun array")
class QueryDeclType(c_ast.NodeVisitor):
	def __init__(self):
		self.result = ArgType.other
	def visit_FuncDecl(self, node):
		self.result = ArgType.fun
	def visit_ArrayDecl(self, node):
		self.result = ArgType.array

class Arg:
	def __init__(self, node):
		self.node = node

	def queryType(self):
		query = QueryDeclType() 	
		query.visit(self.node)
		return query.result

	def shouldRename(self):
		t = self.queryType()
		return not (t == ArgType.fun or t == ArgType.array)

	def show(self):
		if not DEBUG:
			return
		print("name %s" % self.node.name)
		self.node.type.show()
		print("type %r" % self.queryType())

class RewriteTypeDecl(c_ast.NodeVisitor):
	def __init__(self, alias):
		self.alias = alias

	def visit_TypeDecl(self, node):
		node.declname = self.alias

class RenameVars(c_ast.NodeVisitor):
	def __init__(self, init_table):
		self.cur_table = init_table

	def visit_Compound(self, node):
		self.switchTable()
		c_ast.NodeVisitor.generic_visit(self, node)
		self.revertTable()

	def visit_Decl(self, node):
		self.cur_table.register(node.name)
		alias = self.cur_table.alias(node.name)
		P("Decl: %s -> %s" % (node.name, alias))
		node.name = alias
		RewriteTypeDecl(alias).visit(node)
		c_ast.NodeVisitor.generic_visit(self, node)

	def visit_StructRef(self, node):
		alias = self.cur_table.alias(node.name.name)
		P("StructRef: %s -> %s" % (node.name.name, alias))
		node.name.name = alias

	def visit_ID(self, node):
		alias = self.cur_table.alias(node.name)
		P("ID: %s -> %s" % (node.name, alias))
		node.name = alias

	def switchTable(self):
		P("switch table")
		self.cur_table.show()
		new_table = self.cur_table.clone()
		new_table.prev_table = self.cur_table
		self.cur_table = new_table

	def revertTable(self):
		P("revert table")
		self.cur_table = self.cur_table.prev_table

class HasJump(c_ast.NodeVisitor):
	def __init__(self):
		self.result = False

	def visit_Goto(self, n):
		self.result = True

	def visit_Label(self, n):
		self.result = True

class RewriteFun:
	def __init__(self, func):
		self.func = func

		if DEBUG:
			self.func.show()

		self.success = True

		has_jump = HasJump()
		has_jump.visit(self.func)
		if has_jump.result:
			self.success = False
			return

		if self.returnVoid():
			self.success = False
			return

		self.args = []
		self.init_table = NameTable()

		params = []
		if not self.voidArgs():
			params = func.decl.type.args.params	

		for param_decl in params:
			arg = Arg(param_decl)
			self.args.append(arg)

		for arg in self.args:
			name = arg.node.name
			if arg.shouldRename():
				self.init_table.declare(name)	
			else:
				self.init_table.table[name] = Symbol(name, False)

	def returnVoid(self):
		# void f(...)
		return not "void" in self.func.decl.type.type.type.names

	def voidArgs(self):
		args = self.func.decl.type.args

		# f()
		if args == None:
			return True

		# f(a, b, ...)
		if len(args.params) > 1:
			return False

		param = args.params[0]
		query = QueryDeclType()
		query.visit(param)

		# f(...(*g)(...))
		if query.result == ArgType.fun:
			return False

		# f(void)
		if "void" in param.type.type.names:
			return True

	def renameVars(self):
		if not self.success:
			return self

		block_items = self.func.body.block_items
		if not block_items:
			return self

		visitor = RenameVars(self.init_table)
		for x in block_items:
			visitor.visit(x)
		return self

	def insertDeclLines(self):
		if not self.success:
			return self

		block_items = self.func.body.block_items
		if not block_items:
			return self

		for arg in reversed(self.args):
			if arg.shouldRename():
				decl = copy.deepcopy(arg.node)
				alias = self.init_table.alias(arg.node.name)
				decl.name = alias
				RewriteTypeDecl(alias).visit(decl)
				decl.init = c_ast.ID(arg.node.name)
				block_items.insert(0, decl)
		return self

	def macroize(self):
		if not self.success:
			return self

		fun_name = self.func.decl.name
		args = ', '.join(map(lambda arg: arg.node.name, self.args))
		generator = pycparser_ext.CGenerator()
		body_contents = generator.visit(self.func.body).splitlines()[1:-1]
		if not len(body_contents):
			body_contents = [""]
		body = '\n'.join(map(lambda x: "%s \\" % x, body_contents))
		macro = r"""
#define %s(%s) \
do { \
%s
} while(0)
""" % (fun_name, args, body)
		self.func = pycparser_ext.Any(macro)
		return self

	def run(self):
		self.renameVars().insertDeclLines().macroize()

	def returnAST(self):
		return self.func

	def show(self): 
		generator = pycparser_ext.CGenerator()
		print(generator.visit(self.func))
		return self

testcase = r"""
inline void fun(int x, char *y, int (*f)(int), void (*g)(char c), struct T *t, int ys[3])  
{
	int z = *y;
	int *pz = &z;
	int xs[3];
	x = z;
	while (x) {
		int x;
		x = 0;
		x = 0;
		do {
			int x = 0;
			x += x;
		} while (x);
		x += x;
	}
	int alpha;
	if (*y) {
		t->x = f(*y);
	} else {
		g(t->x);
	}
	do {
		struct T t;
		t.x = 1;
	} while (0);
}
"""

testcase_2 = r"""
inline void fun(int x) {}
"""

testcase_3 = r"""
inline int fun(int x) { return x; }
"""

testcase_4 = r"""
inline void fun(int x)
{
	if (1) {
		return;
	}
	while (1) {
		return;
	}
	return;
}
"""

testcase_void1 = r"""
inline void fun(void)
{
	x = 1;
	goto exit;
exit:
	;
}
"""

testcase_void2 = r"""
inline void fun()
{
	x = 1;
}
"""

testcase_void3 = r"""
inline void fun(void (*f)(void))
{
	f();
	x = 1;
}
"""

def test(testcase):
	parser = c_parser.CParser()
	ast = parser.parse(testcase)
	rewrite_fun = RewriteFun(ast.ext[0])
	rewrite_fun.renameVars().show().insertDeclLines().show().macroize().show()

if __name__ == "__main__":
	# test(testcase)
	test(testcase_2)
	test(testcase_3)
	test(testcase_4)
	test(testcase_void1)
	test(testcase_void2)
	test(testcase_void3)
