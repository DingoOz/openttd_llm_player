class LLMIdle extends AIController {
	function Start();
}

function LLMIdle::Start()
{
	while (true) {
		/* Drain the queue so it cannot grow without bound over a long episode. */
		while (AIEventController.IsEventWaiting()) AIEventController.GetNextEvent();
		this.Sleep(74);
	}
}
