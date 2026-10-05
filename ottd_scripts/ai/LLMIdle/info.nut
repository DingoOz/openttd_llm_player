class LLMIdle extends AIInfo {
	function GetAuthor()      { return "ottd-bench"; }
	function GetName()        { return "LLMIdle"; }
	function GetDescription() { return "Placeholder that owns the benchmark company. Every action is issued by the LLMBridge game script."; }
	function GetVersion()     { return 1; }
	function GetDate()        { return "2026-10-04"; }
	function CreateInstance() { return "LLMIdle"; }
	function GetShortName()   { return "LLMI"; }
	function GetAPIVersion()  { return "15"; }
	function UseAsRandomAI()  { return false; }
}

RegisterAI(LLMIdle());
