// Label functions and data from bw1-decomp's config/<version>/symbols.txt (decomp-toolkit format):
//   ?Name@Class@@QAEXXZ = .text:0x00401350; // type:function size:0xB0 scope:global
//
// Run as a headless pre-script, before auto-analysis, so the Microsoft demangler analyzer turns the
// mangled names into namespaces and signatures and the analysis starts from every known function:
//   analyzeHeadless <dir> <project> -import runblack-decrypted.exe \
//       -preScript ApplyBw1Symbols.java /path/to/symbols.txt
// It can also be run on an analysed program; existing names are replaced by the symbols.txt ones.
//
// Functions: disassembled and created at their address, named unless the name is a toolkit placeholder
// (fn_XXXXXXXX). Objects and labels: a primary label, unless the name is a placeholder (lbl_, data_0x ...).
// Sizes and types are not applied.
//@category bw1-decomp

import java.io.BufferedReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;

public class ApplyBw1Symbols extends GhidraScript {

	private static final Pattern LINE = Pattern.compile(
		"^(\\S+)\\s*=\\s*\\.([A-Za-z0-9_.$]+):0x([0-9A-Fa-f]+);\\s*//(.*)$");
	private static final Pattern TYPE = Pattern.compile("\\btype:(\\w+)");
	private static final Pattern PLACEHOLDER = Pattern.compile(
		"^(fn|lbl|data|jumptable|switch)_(0x)?[0-9A-Fa-f]+$");

	@Override
	protected void run() throws Exception {
		String[] args = getScriptArgs();
		if (args.length != 1) {
			throw new IllegalArgumentException("usage: ApplyBw1Symbols.java <symbols.txt>");
		}
		Path path = Path.of(args[0]);
		SymbolTable st = currentProgram.getSymbolTable();
		int functions = 0, labels = 0, skipped = 0, failed = 0;

		try (BufferedReader r = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
			String line;
			while ((line = r.readLine()) != null) {
				monitor.checkCancelled();
				Matcher m = LINE.matcher(line.trim());
				if (!m.matches()) {
					continue;
				}
				String name = m.group(1);
				Address addr = toAddr(Long.parseLong(m.group(3), 16));
				Matcher t = TYPE.matcher(m.group(4));
				String type = t.find() ? t.group(1) : "label";
				boolean placeholder = PLACEHOLDER.matcher(name).matches();
				if (currentProgram.getMemory().getBlock(addr) == null) {
					failed++;
					continue;
				}
				try {
					if (type.equals("function")) {
						if (currentProgram.getListing().getInstructionAt(addr) == null) {
							new DisassembleCommand(addr, null, true).applyTo(currentProgram, monitor);
						}
						Function f = getFunctionAt(addr);
						if (f == null) {
							CreateFunctionCmd cmd = new CreateFunctionCmd(placeholder ? null : name, addr, null,
								placeholder ? SourceType.DEFAULT : SourceType.IMPORTED);
							cmd.applyTo(currentProgram, monitor);
							f = getFunctionAt(addr);
						}
						if (f == null) {
							failed++;
							continue;
						}
						if (!placeholder && !f.getName().equals(name)) {
							f.setName(name, SourceType.IMPORTED);
						}
						functions++;
					}
					else if (placeholder) {
						skipped++;
					}
					else {
						Symbol s = st.createLabel(addr, name, SourceType.IMPORTED);
						s.setPrimary();
						labels++;
					}
				}
				catch (Exception e) {
					failed++;
					if (failed <= 20) {
						printerr("ApplyBw1Symbols: " + name + " @ " + addr + ": " + e.getMessage());
					}
				}
			}
		}
		println(String.format("ApplyBw1Symbols: %d functions, %d labels, %d placeholders skipped, %d failed (%s)",
			functions, labels, skipped, failed, path));
	}
}
