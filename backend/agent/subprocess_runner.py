import json
import logging
import os
import sys

from backend.agent.runner import AgentRunner

logger = logging.getLogger("backend.agent.subprocess_runner")

def format_history_steps(history, step_screenshots):
    steps = []
    current_step = None
    step_counter = 0

    for item in history:
        role = getattr(item, 'role', '')
        parts = getattr(item, 'parts', [])
        if isinstance(item, dict):
            role = item.get('role', '')
            parts = item.get('parts', [])

        if role == 'model':
            thought = ""
            function_calls = []

            for part in parts:
                print(f"DEBUG_PART_DUMP: {type(part)}")
                try: print(f"DEBUG_PART_DIR: {dir(part)}") 
                except: pass
                p_text = getattr(part, 'text', '') if not isinstance(part, dict) else part.get('text', '')
                p_fc = getattr(part, 'function_call', None) if not isinstance(part, dict) else part.get('function_call', None)
                
                print(f"DEBUG_P_TEXT: {type(p_text)} | Value: {repr(p_text)}")
                
                if p_text:
                    thought += str(p_text) + "\n"
                if p_fc:
                    if isinstance(p_fc, dict):
                        fc_name = p_fc.get('name', '')
                        fc_args = p_fc.get('args', {})
                    else:
                        fc_name = getattr(p_fc, 'name', '')
                        fc_args = getattr(p_fc, 'args', {})
                        if hasattr(fc_args, 'to_dict'):
                            fc_args = fc_args.to_dict()
                        elif not isinstance(fc_args, dict):
                            fc_args = dict(fc_args) if fc_args else {}
                    function_calls.append({'name': fc_name, 'args': fc_args})

            if function_calls:
                for fc in function_calls:
                    step_counter += 1
                    shot_name = f"{step_counter:03d}_{fc['name']}.png" if step_counter <= len(step_screenshots) else None
                    current_step = {
                        "step_number": step_counter,
                        "action": fc['name'],
                        "args": fc['args'],
                        "thought": thought.strip(),
                        "url": None,
                        "screenshot": shot_name
                    }
                    steps.append(current_step)
            elif thought.strip():
                step_counter += 1
                current_step = {
                    "step_number": step_counter,
                    "action": "agent_reasoning",
                    "args": {},
                    "thought": thought.strip(),
                    "url": None,
                    "screenshot": None
                }
                steps.append(current_step)

        elif role == 'user' and current_step is not None:
            for part in parts:
                p_fr = getattr(part, 'function_response', None) if not isinstance(part, dict) else part.get('function_response', None)
                if p_fr:
                    resp = getattr(p_fr, 'response', {}) if not isinstance(p_fr, dict) else p_fr.get('response', {})
                    if isinstance(resp, dict) and 'url' in resp:
                        current_step['url'] = resp['url']

    return steps

def main():
    try:
        input_data = sys.stdin.read()
        if not input_data:
            sys.exit(1)
            
        data = json.loads(input_data)
        task_prompt = data["task_prompt"]
        initial_url = data["initial_url"]
        model_name = data["model_name"]
        artifact_dir = data["artifact_dir"]
        
        # from configs but we can just use default screen size here
        from backend.configs import DEFAULT_SCREEN_SIZE
        
        agent_runner = AgentRunner(model_name=model_name)
        agent_out = agent_runner.run(task_prompt, initial_url, DEFAULT_SCREEN_SIZE)
        
        history = agent_out.get("history", [])
        
        # DEBUG DUMP
        try:
            with open(os.path.join(artifact_dir, "history_dump.txt"), "w") as f:
                f.write(str(history))
        except Exception:
            pass
            
        step_screenshots = agent_out.get("screenshots", [])
        agent_claim = agent_out.get("agent_claim", "")
        
        steps = format_history_steps(history, step_screenshots)
        
        shot_dir = os.path.join(artifact_dir, "screenshots")
        os.makedirs(shot_dir, exist_ok=True)
        shot_names = []
        for idx, (action_name, img_data) in enumerate(step_screenshots, 1):
            if img_data:
                name = f"{idx:03d}_{action_name}.png"
                with open(os.path.join(shot_dir, name), "wb") as f:
                    f.write(img_data)
                shot_names.append(name)
        
        result = {
            "success": True,
            "agent_claim": agent_claim,
            "steps": steps,
            "screenshots": shot_names
        }
        
        result_file = os.path.join(artifact_dir, "agent_result.json")
        with open(result_file, "w") as f:
            json.dump(result, f, indent=2)
            
        sys.exit(0)
    except Exception as e:
        import traceback
        logger.error(f"Error in subprocess runner: {e}\n{traceback.format_exc()}")
        # Not writing agent_result.json means it will fail
        sys.exit(1)

if __name__ == "__main__":
    main()
