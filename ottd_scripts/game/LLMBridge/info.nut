class LLMBridge extends GSInfo {
	function GetAuthor()      { return "ottd-bench"; }
	function GetName()        { return "LLMBridge"; }
	function GetDescription() { return "JSON-RPC bridge over the admin port: lets an external harness observe the game and act as a company."; }
	function GetVersion()     { return 1; }
	function GetDate()        { return "2026-10-04"; }
	function CreateInstance() { return "LLMBridge"; }
	function GetShortName()   { return "LLMB"; }
	function GetAPIVersion()  { return "15"; }
	function GetURL()         { return ""; }
}

RegisterGS(LLMBridge());
