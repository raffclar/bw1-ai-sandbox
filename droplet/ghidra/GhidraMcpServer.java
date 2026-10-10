// GhidraMCP's HTTP API, served from a headless Ghidra (analyzeHeadless -process ... -postScript).
//
// A port of the request handlers of LaurieWired/GhidraMCP's GhidraMCPPlugin.java at tag 1.4
// (commit 27f316f80139e2d5dec882519a1bdf4aa46ac04c, Apache-2.0), so that the bridge of the same tag,
// bridge_mcp_ghidra.py, works unchanged. Endpoints, parameters and response texts are the plugin's.
// Differences, all forced by having no GUI:
//   * the program is the one analyzeHeadless opened, not the CodeBrowser's current program;
//   * get_current_address / get_current_function answer "Code viewer service not available", which is
//     what the plugin says when there is no code viewer;
//   * edits run on the HTTP threads in transactions instead of on the Swing thread;
//   * read-only mode (third argument "ro", analyzeHeadless -readOnly): every editing endpoint answers
//     "Refused: the Ghidra project is read-only" and changes nothing;
//   * read-write mode ("rw"): edits are saved to the project every SAVE_MINUTES and when the service
//     stops (SIGTERM ends the script, and analyzeHeadless then saves the program);
//   * the server binds to the given address only, never to every interface.
//
// Usage:
//   analyzeHeadless <dir> <project> -process <file> -noanalysis [-readOnly] \
//       -scriptPath <this folder> -postScript GhidraMcpServer.java <bind address> <port> rw|ro
//@category bw1-decomp

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantReadWriteLock;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import ghidra.app.cmd.function.ApplyFunctionSignatureCmd;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.app.util.parser.FunctionSignatureParser;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.GlobalNamespace;
import ghidra.program.model.data.DataType;
import ghidra.program.model.data.DataTypeManager;
import ghidra.program.model.data.FunctionDefinitionDataType;
import ghidra.program.model.data.PointerDataType;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.pcode.HighFunction;
import ghidra.program.model.pcode.HighFunctionDBUtil;
import ghidra.program.model.pcode.HighFunctionDBUtil.ReturnCommitOption;
import ghidra.program.model.pcode.HighSymbol;
import ghidra.program.model.pcode.HighVariable;
import ghidra.program.model.pcode.LocalSymbolMap;
import ghidra.program.model.symbol.*;
import ghidra.util.Msg;
import ghidra.util.task.TaskMonitor;

public class GhidraMcpServer extends GhidraScript {

	private static final int THREADS = 4;
	private static final int SAVE_MINUTES = 5;
	private static final String READ_ONLY = "Refused: the Ghidra project is read-only";

	private Program program;
	private boolean readOnly;
	private HttpServer server;
	// Requests share the read lock (Ghidra's own transactions order the edits); saving takes the write
	// lock, because a save must not overlap an open transaction.
	private final ReentrantReadWriteLock lock = new ReentrantReadWriteLock();
	private final ThreadLocal<DecompInterface> decompilers = new ThreadLocal<>();

	@Override
	protected void run() throws Exception {
		String[] args = getScriptArgs();
		if (args.length != 3 || !(args[2].equals("rw") || args[2].equals("ro"))) {
			throw new IllegalArgumentException("usage: GhidraMcpServer.java <bind address> <port> rw|ro");
		}
		program = currentProgram;
		readOnly = args[2].equals("ro");
		InetSocketAddress bind = new InetSocketAddress(InetAddress.getByName(args[0]), Integer.parseInt(args[1]));

		server = HttpServer.create(bind, 0);
		register();
		ExecutorService pool = Executors.newFixedThreadPool(THREADS);
		server.setExecutor(pool);
		server.start();
		println("GhidraMCP HTTP server (headless) on " + bind + " for " + program.getName() +
			(readOnly ? " (read-only)" : " (read-write, saved every " + SAVE_MINUTES + " min)"));

		CountDownLatch stop = new CountDownLatch(1);
		installStopSignal(stop);
		ScheduledExecutorService saver = Executors.newSingleThreadScheduledExecutor();
		if (!readOnly) {
			saver.scheduleWithFixedDelay(this::saveIfChanged, SAVE_MINUTES, SAVE_MINUTES, TimeUnit.MINUTES);
		}
		stop.await();

		println("GhidraMCP HTTP server stopping");
		server.stop(1);
		pool.shutdown();
		pool.awaitTermination(30, TimeUnit.SECONDS);
		saver.shutdownNow();
		// analyzeHeadless saves the program after the script returns unless -readOnly was given.
	}

	/** SIGTERM / SIGINT end the script normally, so analyzeHeadless saves and releases the project. */
	private void installStopSignal(CountDownLatch stop) {
		try {
			for (String sig : new String[] { "TERM", "INT" }) {
				sun.misc.Signal.handle(new sun.misc.Signal(sig), s -> stop.countDown());
			}
		}
		catch (Throwable t) {
			Msg.warn(this, "no signal handler (" + t + "); edits are saved only every " + SAVE_MINUTES + " min");
		}
	}

	private void saveIfChanged() {
		lock.writeLock().lock();
		try {
			if (program.isChanged() && program.canSave()) {
				program.save("GhidraMCP edits", TaskMonitor.DUMMY);
				Msg.info(this, "saved GhidraMCP edits");
			}
		}
		catch (Exception e) {
			Msg.error(this, "saving GhidraMCP edits failed", e);
		}
		finally {
			lock.writeLock().unlock();
		}
	}

	// ----------------------------------------------------------------------------------
	// Endpoints (GhidraMCPPlugin.startServer)
	// ----------------------------------------------------------------------------------

	private interface Body {
		String handle(HttpExchange exchange) throws IOException;
	}

	private void context(String path, Body body) {
		HttpHandler h = exchange -> {
			String response;
			lock.readLock().lock();
			try {
				response = body.handle(exchange);
			}
			catch (Exception e) {
				Msg.error(this, "GhidraMCP " + path + " failed", e);
				response = "Error: " + e.getMessage();
			}
			finally {
				lock.readLock().unlock();
			}
			sendResponse(exchange, response);
		};
		server.createContext(path, h);
	}

	private void register() {
		context("/methods", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return getAllFunctionNames(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/classes", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return getAllClassNames(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/decompile", ex -> {
			String name = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
			return decompileFunctionByName(name);
		});
		context("/renameFunction", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			return renameFunction(p.get("oldName"), p.get("newName")) ? "Renamed successfully" : "Rename failed";
		});
		context("/renameData", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			renameDataAtAddress(p.get("address"), p.get("newName"));
			return "Rename data attempted";
		});
		context("/renameVariable", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			return renameVariableInFunction(p.get("functionName"), p.get("oldName"), p.get("newName"));
		});
		context("/segments", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return listSegments(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/imports", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return listImports(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/exports", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return listExports(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/namespaces", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return listNamespaces(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/data", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return listDefinedData(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/searchFunctions", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return searchFunctionsByName(q.get("query"), parseIntOrDefault(q.get("offset"), 0),
				parseIntOrDefault(q.get("limit"), 100));
		});
		context("/get_function_by_address", ex -> getFunctionByAddress(parseQueryParams(ex).get("address")));
		context("/get_current_address", ex -> "Code viewer service not available");
		context("/get_current_function", ex -> "Code viewer service not available");
		context("/list_functions", ex -> listFunctions());
		context("/decompile_function", ex -> decompileFunctionByAddress(parseQueryParams(ex).get("address")));
		context("/disassemble_function", ex -> disassembleFunction(parseQueryParams(ex).get("address")));
		context("/set_decompiler_comment", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			return setCommentAtAddress(p.get("address"), p.get("comment"), CommentType.PRE, "Set decompiler comment")
					? "Comment set successfully" : "Failed to set comment";
		});
		context("/set_disassembly_comment", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			return setCommentAtAddress(p.get("address"), p.get("comment"), CommentType.EOL, "Set disassembly comment")
					? "Comment set successfully" : "Failed to set comment";
		});
		context("/rename_function_by_address", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			return renameFunctionByAddress(p.get("function_address"), p.get("new_name"))
					? "Function renamed successfully" : "Failed to rename function";
		});
		context("/set_function_prototype", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			StringBuilder err = new StringBuilder();
			boolean ok = setFunctionPrototype(p.get("function_address"), p.get("prototype"), err);
			if (ok) {
				String msg = "Function prototype set successfully";
				if (err.length() > 0) msg += "\n\nWarnings/Debug Info:\n" + err;
				return msg;
			}
			return "Failed to set function prototype: " + err;
		});
		context("/set_local_variable_type", ex -> {
			Map<String, String> p = parsePostParams(ex);
			if (readOnly) return READ_ONLY;
			return setLocalVariableTypeResponse(p.get("function_address"), p.get("variable_name"), p.get("new_type"));
		});
		context("/xrefs_to", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return getXrefsTo(q.get("address"), parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100));
		});
		context("/xrefs_from", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return getXrefsFrom(q.get("address"), parseIntOrDefault(q.get("offset"), 0),
				parseIntOrDefault(q.get("limit"), 100));
		});
		context("/function_xrefs", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return getFunctionXrefs(q.get("name"), parseIntOrDefault(q.get("offset"), 0),
				parseIntOrDefault(q.get("limit"), 100));
		});
		context("/strings", ex -> {
			Map<String, String> q = parseQueryParams(ex);
			return listDefinedStrings(parseIntOrDefault(q.get("offset"), 0), parseIntOrDefault(q.get("limit"), 100),
				q.get("filter"));
		});
	}

	// ----------------------------------------------------------------------------------
	// Listings
	// ----------------------------------------------------------------------------------

	private String getAllFunctionNames(int offset, int limit) {
		List<String> names = new ArrayList<>();
		for (Function f : program.getFunctionManager().getFunctions(true)) {
			names.add(f.getName());
		}
		return paginateList(names, offset, limit);
	}

	private String getAllClassNames(int offset, int limit) {
		Set<String> classNames = new HashSet<>();
		for (Symbol symbol : program.getSymbolTable().getAllSymbols(true)) {
			Namespace ns = symbol.getParentNamespace();
			if (ns != null && !ns.isGlobal()) {
				classNames.add(ns.getName());
			}
		}
		List<String> sorted = new ArrayList<>(classNames);
		Collections.sort(sorted);
		return paginateList(sorted, offset, limit);
	}

	private String listSegments(int offset, int limit) {
		List<String> lines = new ArrayList<>();
		for (MemoryBlock block : program.getMemory().getBlocks()) {
			lines.add(String.format("%s: %s - %s", block.getName(), block.getStart(), block.getEnd()));
		}
		return paginateList(lines, offset, limit);
	}

	private String listImports(int offset, int limit) {
		List<String> lines = new ArrayList<>();
		for (Symbol symbol : program.getSymbolTable().getExternalSymbols()) {
			lines.add(symbol.getName() + " -> " + symbol.getAddress());
		}
		return paginateList(lines, offset, limit);
	}

	private String listExports(int offset, int limit) {
		List<String> lines = new ArrayList<>();
		SymbolIterator it = program.getSymbolTable().getAllSymbols(true);
		while (it.hasNext()) {
			Symbol s = it.next();
			if (s.isExternalEntryPoint()) {
				lines.add(s.getName() + " -> " + s.getAddress());
			}
		}
		return paginateList(lines, offset, limit);
	}

	private String listNamespaces(int offset, int limit) {
		Set<String> namespaces = new HashSet<>();
		for (Symbol symbol : program.getSymbolTable().getAllSymbols(true)) {
			Namespace ns = symbol.getParentNamespace();
			if (ns != null && !(ns instanceof GlobalNamespace)) {
				namespaces.add(ns.getName());
			}
		}
		List<String> sorted = new ArrayList<>(namespaces);
		Collections.sort(sorted);
		return paginateList(sorted, offset, limit);
	}

	private String listDefinedData(int offset, int limit) {
		List<String> lines = new ArrayList<>();
		for (MemoryBlock block : program.getMemory().getBlocks()) {
			DataIterator it = program.getListing().getDefinedData(block.getStart(), true);
			while (it.hasNext()) {
				Data data = it.next();
				if (!block.contains(data.getAddress())) {
					break; // the plugin keeps iterating and skips these; the output is the same
				}
				String label = data.getLabel() != null ? data.getLabel() : "(unnamed)";
				String valRepr = data.getDefaultValueRepresentation();
				lines.add(String.format("%s: %s = %s", data.getAddress(), escapeNonAscii(label),
					escapeNonAscii(valRepr)));
			}
		}
		return paginateList(lines, offset, limit);
	}

	private String searchFunctionsByName(String searchTerm, int offset, int limit) {
		if (searchTerm == null || searchTerm.isEmpty()) return "Search term is required";
		List<String> matches = new ArrayList<>();
		String needle = searchTerm.toLowerCase();
		for (Function func : program.getFunctionManager().getFunctions(true)) {
			String name = func.getName();
			if (name.toLowerCase().contains(needle)) {
				matches.add(String.format("%s @ %s", name, func.getEntryPoint()));
			}
		}
		Collections.sort(matches);
		if (matches.isEmpty()) {
			return "No functions matching '" + searchTerm + "'";
		}
		return paginateList(matches, offset, limit);
	}

	private String listFunctions() {
		StringBuilder result = new StringBuilder();
		for (Function func : program.getFunctionManager().getFunctions(true)) {
			result.append(String.format("%s at %s\n", func.getName(), func.getEntryPoint()));
		}
		return result.toString();
	}

	private String listDefinedStrings(int offset, int limit, String filter) {
		List<String> lines = new ArrayList<>();
		DataIterator dataIt = program.getListing().getDefinedData(true);
		while (dataIt.hasNext()) {
			Data data = dataIt.next();
			if (data != null && isStringData(data)) {
				String value = data.getValue() != null ? data.getValue().toString() : "";
				if (filter == null || value.toLowerCase().contains(filter.toLowerCase())) {
					lines.add(String.format("%s: \"%s\"", data.getAddress(), escapeString(value)));
				}
			}
		}
		return paginateList(lines, offset, limit);
	}

	private boolean isStringData(Data data) {
		String typeName = data.getDataType().getName().toLowerCase();
		return typeName.contains("string") || typeName.contains("char") || typeName.equals("unicode");
	}

	// ----------------------------------------------------------------------------------
	// Functions, decompiler, disassembly
	// ----------------------------------------------------------------------------------

	private DecompInterface decompiler() {
		DecompInterface d = decompilers.get();
		if (d == null) {
			d = new DecompInterface();
			d.openProgram(program);
			decompilers.set(d);
		}
		return d;
	}

	private String decompile(Function func, int timeout) {
		DecompileResults result = decompiler().decompileFunction(func, timeout, TaskMonitor.DUMMY);
		return (result != null && result.decompileCompleted()) ? result.getDecompiledFunction().getC()
				: "Decompilation failed";
	}

	private String decompileFunctionByName(String name) {
		for (Function func : program.getFunctionManager().getFunctions(true)) {
			if (func.getName().equals(name)) {
				return decompile(func, 30);
			}
		}
		return "Function not found";
	}

	private Function getFunctionForAddress(Address addr) {
		Function func = program.getFunctionManager().getFunctionAt(addr);
		if (func == null) {
			func = program.getFunctionManager().getFunctionContaining(addr);
		}
		return func;
	}

	private String getFunctionByAddress(String addressStr) {
		if (addressStr == null || addressStr.isEmpty()) return "Address is required";
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			Function func = program.getFunctionManager().getFunctionAt(addr);
			if (func == null) return "No function found at address " + addressStr;
			return String.format("Function: %s at %s\nSignature: %s\nEntry: %s\nBody: %s - %s", func.getName(),
				func.getEntryPoint(), func.getSignature(), func.getEntryPoint(), func.getBody().getMinAddress(),
				func.getBody().getMaxAddress());
		}
		catch (Exception e) {
			return "Error getting function: " + e.getMessage();
		}
	}

	private String decompileFunctionByAddress(String addressStr) {
		if (addressStr == null || addressStr.isEmpty()) return "Address is required";
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			Function func = getFunctionForAddress(addr);
			if (func == null) return "No function found at or containing address " + addressStr;
			return decompile(func, 30);
		}
		catch (Exception e) {
			return "Error decompiling function: " + e.getMessage();
		}
	}

	private String disassembleFunction(String addressStr) {
		if (addressStr == null || addressStr.isEmpty()) return "Address is required";
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			Function func = getFunctionForAddress(addr);
			if (func == null) return "No function found at or containing address " + addressStr;
			StringBuilder result = new StringBuilder();
			Listing listing = program.getListing();
			Address end = func.getBody().getMaxAddress();
			InstructionIterator instructions = listing.getInstructions(func.getEntryPoint(), true);
			while (instructions.hasNext()) {
				Instruction instr = instructions.next();
				if (instr.getAddress().compareTo(end) > 0) {
					break;
				}
				String comment = listing.getComment(CommentType.EOL, instr.getAddress());
				comment = (comment != null) ? "; " + comment : "";
				result.append(String.format("%s: %s %s\n", instr.getAddress(), instr.toString(), comment));
			}
			return result.toString();
		}
		catch (Exception e) {
			return "Error disassembling function: " + e.getMessage();
		}
	}

	// ----------------------------------------------------------------------------------
	// Xrefs
	// ----------------------------------------------------------------------------------

	private String getXrefsTo(String addressStr, int offset, int limit) {
		if (addressStr == null || addressStr.isEmpty()) return "Address is required";
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			ReferenceIterator refIter = program.getReferenceManager().getReferencesTo(addr);
			List<String> refs = new ArrayList<>();
			while (refIter.hasNext()) {
				Reference ref = refIter.next();
				Address fromAddr = ref.getFromAddress();
				Function fromFunc = program.getFunctionManager().getFunctionContaining(fromAddr);
				String funcInfo = (fromFunc != null) ? " in " + fromFunc.getName() : "";
				refs.add(String.format("From %s%s [%s]", fromAddr, funcInfo, ref.getReferenceType().getName()));
			}
			return paginateList(refs, offset, limit);
		}
		catch (Exception e) {
			return "Error getting references to address: " + e.getMessage();
		}
	}

	private String getXrefsFrom(String addressStr, int offset, int limit) {
		if (addressStr == null || addressStr.isEmpty()) return "Address is required";
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			Reference[] references = program.getReferenceManager().getReferencesFrom(addr);
			List<String> refs = new ArrayList<>();
			for (Reference ref : references) {
				Address toAddr = ref.getToAddress();
				String targetInfo = "";
				Function toFunc = program.getFunctionManager().getFunctionAt(toAddr);
				if (toFunc != null) {
					targetInfo = " to function " + toFunc.getName();
				}
				else {
					Data data = program.getListing().getDataAt(toAddr);
					if (data != null) {
						targetInfo = " to data " + (data.getLabel() != null ? data.getLabel() : data.getPathName());
					}
				}
				refs.add(String.format("To %s%s [%s]", toAddr, targetInfo, ref.getReferenceType().getName()));
			}
			return paginateList(refs, offset, limit);
		}
		catch (Exception e) {
			return "Error getting references from address: " + e.getMessage();
		}
	}

	private String getFunctionXrefs(String functionName, int offset, int limit) {
		if (functionName == null || functionName.isEmpty()) return "Function name is required";
		try {
			List<String> refs = new ArrayList<>();
			FunctionManager funcManager = program.getFunctionManager();
			for (Function function : funcManager.getFunctions(true)) {
				if (function.getName().equals(functionName)) {
					ReferenceIterator refIter = program.getReferenceManager().getReferencesTo(function.getEntryPoint());
					while (refIter.hasNext()) {
						Reference ref = refIter.next();
						Address fromAddr = ref.getFromAddress();
						Function fromFunc = funcManager.getFunctionContaining(fromAddr);
						String funcInfo = (fromFunc != null) ? " in " + fromFunc.getName() : "";
						refs.add(String.format("From %s%s [%s]", fromAddr, funcInfo, ref.getReferenceType().getName()));
					}
				}
			}
			if (refs.isEmpty()) {
				return "No references found to function: " + functionName;
			}
			return paginateList(refs, offset, limit);
		}
		catch (Exception e) {
			return "Error getting function references: " + e.getMessage();
		}
	}

	// ----------------------------------------------------------------------------------
	// Edits (refused in read-only mode by the endpoints above)
	// ----------------------------------------------------------------------------------

	private boolean renameFunction(String oldName, String newName) {
		int tx = program.startTransaction("Rename function via HTTP");
		boolean ok = false;
		try {
			for (Function func : program.getFunctionManager().getFunctions(true)) {
				if (func.getName().equals(oldName)) {
					func.setName(newName, SourceType.USER_DEFINED);
					ok = true;
					break;
				}
			}
		}
		catch (Exception e) {
			Msg.error(this, "Error renaming function", e);
		}
		finally {
			ok = program.endTransaction(tx, ok);
		}
		return ok;
	}

	private void renameDataAtAddress(String addressStr, String newName) {
		int tx = program.startTransaction("Rename data");
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			Data data = program.getListing().getDefinedDataAt(addr);
			if (data != null) {
				SymbolTable symTable = program.getSymbolTable();
				Symbol symbol = symTable.getPrimarySymbol(addr);
				if (symbol != null) {
					symbol.setName(newName, SourceType.USER_DEFINED);
				}
				else {
					symTable.createLabel(addr, newName, SourceType.USER_DEFINED);
				}
			}
		}
		catch (Exception e) {
			Msg.error(this, "Rename data error", e);
		}
		finally {
			program.endTransaction(tx, true);
		}
	}

	private String renameVariableInFunction(String functionName, String oldVarName, String newVarName) {
		Function func = null;
		for (Function f : program.getFunctionManager().getFunctions(true)) {
			if (f.getName().equals(functionName)) {
				func = f;
				break;
			}
		}
		if (func == null) return "Function not found";

		DecompileResults result = decompiler().decompileFunction(func, 30, TaskMonitor.DUMMY);
		if (result == null || !result.decompileCompleted()) return "Decompilation failed";
		HighFunction highFunction = result.getHighFunction();
		if (highFunction == null) return "Decompilation failed (no high function)";
		LocalSymbolMap localSymbolMap = highFunction.getLocalSymbolMap();
		if (localSymbolMap == null) return "Decompilation failed (no local symbol map)";

		HighSymbol highSymbol = null;
		Iterator<HighSymbol> symbols = localSymbolMap.getSymbols();
		while (symbols.hasNext()) {
			HighSymbol symbol = symbols.next();
			String symbolName = symbol.getName();
			if (symbolName.equals(oldVarName)) {
				highSymbol = symbol;
			}
			if (symbolName.equals(newVarName)) {
				return "Error: A variable with name '" + newVarName + "' already exists in this function";
			}
		}
		if (highSymbol == null) return "Variable not found";

		boolean commitRequired = checkFullCommit(highSymbol, highFunction);
		boolean ok = false;
		int tx = program.startTransaction("Rename variable");
		try {
			if (commitRequired) {
				HighFunctionDBUtil.commitParamsToDatabase(highFunction, false, ReturnCommitOption.NO_COMMIT,
					func.getSignatureSource());
			}
			HighFunctionDBUtil.updateDBVariable(highSymbol, newVarName, null, SourceType.USER_DEFINED);
			ok = true;
		}
		catch (Exception e) {
			Msg.error(this, "Failed to rename variable", e);
		}
		finally {
			ok = program.endTransaction(tx, true) && ok;
		}
		return ok ? "Variable renamed" : "Failed to rename variable";
	}

	/** AbstractDecompilerAction.checkFullCommit (protected there), as the plugin copies it. */
	private static boolean checkFullCommit(HighSymbol highSymbol, HighFunction hfunction) {
		if (highSymbol != null && !highSymbol.isParameter()) {
			return false;
		}
		Function function = hfunction.getFunction();
		Parameter[] parameters = function.getParameters();
		LocalSymbolMap localSymbolMap = hfunction.getLocalSymbolMap();
		int numParams = localSymbolMap.getNumParams();
		if (numParams != parameters.length) {
			return true;
		}
		for (int i = 0; i < numParams; i++) {
			HighSymbol param = localSymbolMap.getParamSymbol(i);
			if (param.getCategoryIndex() != i) {
				return true;
			}
			VariableStorage storage = param.getStorage();
			if (0 != storage.compareTo(parameters[i].getVariableStorage())) {
				return true;
			}
		}
		return false;
	}

	private boolean setCommentAtAddress(String addressStr, String comment, CommentType type, String txName) {
		if (addressStr == null || addressStr.isEmpty() || comment == null) return false;
		boolean ok = false;
		int tx = program.startTransaction(txName);
		try {
			Address addr = program.getAddressFactory().getAddress(addressStr);
			program.getListing().setComment(addr, type, comment);
			ok = true;
		}
		catch (Exception e) {
			Msg.error(this, "Error setting " + txName.toLowerCase(), e);
		}
		finally {
			ok = program.endTransaction(tx, ok);
		}
		return ok;
	}

	private boolean renameFunctionByAddress(String functionAddrStr, String newName) {
		if (functionAddrStr == null || functionAddrStr.isEmpty() || newName == null || newName.isEmpty()) {
			return false;
		}
		boolean ok = false;
		int tx = program.startTransaction("Rename function by address");
		try {
			Address addr = program.getAddressFactory().getAddress(functionAddrStr);
			Function func = getFunctionForAddress(addr);
			if (func == null) {
				Msg.error(this, "Could not find function at address: " + functionAddrStr);
			}
			else {
				func.setName(newName, SourceType.USER_DEFINED);
				ok = true;
			}
		}
		catch (Exception e) {
			Msg.error(this, "Error renaming function by address", e);
		}
		finally {
			program.endTransaction(tx, ok);
		}
		return ok;
	}

	private boolean setFunctionPrototype(String functionAddrStr, String prototype, StringBuilder errorMessage) {
		if (functionAddrStr == null || functionAddrStr.isEmpty()) {
			errorMessage.append("Function address is required");
			return false;
		}
		if (prototype == null || prototype.isEmpty()) {
			errorMessage.append("Function prototype is required");
			return false;
		}
		try {
			Address addr = program.getAddressFactory().getAddress(functionAddrStr);
			Function func = getFunctionForAddress(addr);
			if (func == null) {
				errorMessage.append("Could not find function at address: " + functionAddrStr);
				return false;
			}
			int txComment = program.startTransaction("Add prototype comment");
			try {
				program.getListing().setComment(func.getEntryPoint(), CommentType.PLATE,
					"Setting prototype: " + prototype);
			}
			finally {
				program.endTransaction(txComment, true);
			}

			boolean ok = false;
			int txProto = program.startTransaction("Set function prototype");
			try {
				DataTypeManager dtm = program.getDataTypeManager();
				FunctionSignatureParser parser = new FunctionSignatureParser(dtm, null);
				FunctionDefinitionDataType sig = parser.parse(null, prototype);
				if (sig == null) {
					errorMessage.append("Failed to parse function prototype");
					return false;
				}
				ApplyFunctionSignatureCmd cmd = new ApplyFunctionSignatureCmd(addr, sig, SourceType.USER_DEFINED);
				if (cmd.applyTo(program, TaskMonitor.DUMMY)) {
					ok = true;
				}
				else {
					errorMessage.append("Command failed: " + cmd.getStatusMsg());
				}
			}
			catch (Exception e) {
				errorMessage.append("Error applying function signature: " + e.getMessage());
			}
			finally {
				program.endTransaction(txProto, ok);
			}
			return ok;
		}
		catch (Exception e) {
			errorMessage.append("Error setting function prototype: " + e.getMessage());
			return false;
		}
	}

	private String setLocalVariableTypeResponse(String functionAddress, String variableName, String newType) {
		StringBuilder responseMsg = new StringBuilder();
		responseMsg.append("Setting variable type: ").append(variableName).append(" to ").append(newType)
				.append(" in function at ").append(functionAddress).append("\n\n");
		DataTypeManager dtm = program.getDataTypeManager();
		if (newType != null) {
			DataType directType = findDataTypeByNameInAllCategories(dtm, newType);
			if (directType != null) {
				responseMsg.append("Found type: ").append(directType.getPathName()).append("\n");
			}
			else if (newType.startsWith("P") && newType.length() > 1) {
				String baseTypeName = newType.substring(1);
				DataType baseType = findDataTypeByNameInAllCategories(dtm, baseTypeName);
				if (baseType != null) {
					responseMsg.append("Found base type for pointer: ").append(baseType.getPathName()).append("\n");
				}
				else {
					responseMsg.append("Base type not found for pointer: ").append(baseTypeName).append("\n");
				}
			}
			else {
				responseMsg.append("Type not found directly: ").append(newType).append("\n");
			}
		}
		boolean success = setLocalVariableType(functionAddress, variableName, newType);
		responseMsg.append("\nResult: ").append(success ? "Variable type set successfully" : "Failed to set variable type");
		return responseMsg.toString();
	}

	private boolean setLocalVariableType(String functionAddrStr, String variableName, String newType) {
		if (functionAddrStr == null || functionAddrStr.isEmpty() || variableName == null || variableName.isEmpty() ||
			newType == null || newType.isEmpty()) {
			return false;
		}
		try {
			Address addr = program.getAddressFactory().getAddress(functionAddrStr);
			Function func = getFunctionForAddress(addr);
			if (func == null) return false;
			DecompInterface decomp = new DecompInterface();
			decomp.openProgram(program);
			decomp.setSimplificationStyle("decompile");
			DecompileResults results = decomp.decompileFunction(func, 60, TaskMonitor.DUMMY);
			decomp.dispose();
			if (results == null || !results.decompileCompleted()) return false;
			HighFunction highFunction = results.getHighFunction();
			if (highFunction == null) return false;
			HighSymbol symbol = null;
			Iterator<HighSymbol> symbols = highFunction.getLocalSymbolMap().getSymbols();
			while (symbols.hasNext()) {
				HighSymbol s = symbols.next();
				if (s.getName().equals(variableName)) {
					symbol = s;
					break;
				}
			}
			if (symbol == null) return false;
			HighVariable highVar = symbol.getHighVariable();
			if (highVar == null) return false;
			DataType dataType = resolveDataType(program.getDataTypeManager(), newType);
			if (dataType == null) return false;

			boolean ok = false;
			int tx = program.startTransaction("Set variable type");
			try {
				HighFunctionDBUtil.updateDBVariable(symbol, symbol.getName(), dataType, SourceType.USER_DEFINED);
				ok = true;
			}
			catch (Exception e) {
				Msg.error(this, "Error setting variable type: " + e.getMessage());
			}
			finally {
				program.endTransaction(tx, ok);
			}
			return ok;
		}
		catch (Exception e) {
			Msg.error(this, "Error setting variable type: " + e.getMessage());
			return false;
		}
	}

	private DataType resolveDataType(DataTypeManager dtm, String typeName) {
		DataType dataType = findDataTypeByNameInAllCategories(dtm, typeName);
		if (dataType != null) {
			return dataType;
		}
		if (typeName.startsWith("P") && typeName.length() > 1) {
			String baseTypeName = typeName.substring(1);
			if (baseTypeName.equals("VOID")) {
				return new PointerDataType(dtm.getDataType("/void"));
			}
			DataType baseType = findDataTypeByNameInAllCategories(dtm, baseTypeName);
			if (baseType != null) {
				return new PointerDataType(baseType);
			}
			return new PointerDataType(dtm.getDataType("/void"));
		}
		switch (typeName.toLowerCase()) {
			case "int":
			case "long":
				return dtm.getDataType("/int");
			case "uint":
			case "unsigned int":
			case "unsigned long":
			case "dword":
				return dtm.getDataType("/uint");
			case "short":
				return dtm.getDataType("/short");
			case "ushort":
			case "unsigned short":
			case "word":
				return dtm.getDataType("/ushort");
			case "char":
			case "byte":
				return dtm.getDataType("/char");
			case "uchar":
			case "unsigned char":
				return dtm.getDataType("/uchar");
			case "longlong":
			case "__int64":
				return dtm.getDataType("/longlong");
			case "ulonglong":
			case "unsigned __int64":
				return dtm.getDataType("/ulonglong");
			case "bool":
			case "boolean":
				return dtm.getDataType("/bool");
			case "void":
				return dtm.getDataType("/void");
			default:
				DataType directType = dtm.getDataType("/" + typeName);
				if (directType != null) {
					return directType;
				}
				return dtm.getDataType("/int");
		}
	}

	private DataType findDataTypeByNameInAllCategories(DataTypeManager dtm, String typeName) {
		DataType result = searchByNameInAllCategories(dtm, typeName);
		if (result != null) {
			return result;
		}
		return searchByNameInAllCategories(dtm, typeName.toLowerCase());
	}

	private DataType searchByNameInAllCategories(DataTypeManager dtm, String name) {
		Iterator<DataType> allTypes = dtm.getAllDataTypes();
		while (allTypes.hasNext()) {
			DataType dt = allTypes.next();
			if (dt.getName().equals(name) || dt.getName().equalsIgnoreCase(name)) {
				return dt;
			}
		}
		return null;
	}

	// ----------------------------------------------------------------------------------
	// Request and response helpers (as the plugin's)
	// ----------------------------------------------------------------------------------

	private Map<String, String> parseQueryParams(HttpExchange exchange) {
		return parsePairs(exchange.getRequestURI().getRawQuery());
	}

	private Map<String, String> parsePostParams(HttpExchange exchange) throws IOException {
		return parsePairs(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
	}

	private Map<String, String> parsePairs(String s) {
		Map<String, String> result = new HashMap<>();
		if (s == null) {
			return result;
		}
		for (String p : s.split("&")) {
			String[] kv = p.split("=");
			if (kv.length == 2) {
				try {
					result.put(URLDecoder.decode(kv[0], StandardCharsets.UTF_8),
						URLDecoder.decode(kv[1], StandardCharsets.UTF_8));
				}
				catch (Exception e) {
					Msg.error(this, "Error decoding URL parameter", e);
				}
			}
		}
		return result;
	}

	private String paginateList(List<String> items, int offset, int limit) {
		int start = Math.max(0, offset);
		int end = Math.min(items.size(), offset + limit);
		if (start >= items.size()) {
			return "";
		}
		return String.join("\n", items.subList(start, end));
	}

	private int parseIntOrDefault(String val, int defaultValue) {
		if (val == null) return defaultValue;
		try {
			return Integer.parseInt(val);
		}
		catch (NumberFormatException e) {
			return defaultValue;
		}
	}

	private String escapeNonAscii(String input) {
		if (input == null) return "";
		StringBuilder sb = new StringBuilder();
		for (char c : input.toCharArray()) {
			if (c >= 32 && c < 127) {
				sb.append(c);
			}
			else {
				sb.append("\\x").append(Integer.toHexString(c & 0xFF));
			}
		}
		return sb.toString();
	}

	private String escapeString(String input) {
		if (input == null) return "";
		StringBuilder sb = new StringBuilder();
		for (int i = 0; i < input.length(); i++) {
			char c = input.charAt(i);
			if (c >= 32 && c < 127) sb.append(c);
			else if (c == '\n') sb.append("\\n");
			else if (c == '\r') sb.append("\\r");
			else if (c == '\t') sb.append("\\t");
			else sb.append(String.format("\\x%02x", (int) c & 0xFF));
		}
		return sb.toString();
	}

	private void sendResponse(HttpExchange exchange, String response) throws IOException {
		byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
		exchange.getResponseHeaders().set("Content-Type", "text/plain; charset=utf-8");
		exchange.sendResponseHeaders(200, bytes.length);
		try (OutputStream os = exchange.getResponseBody()) {
			os.write(bytes);
		}
	}
}
