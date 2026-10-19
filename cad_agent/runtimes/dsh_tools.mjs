// Trusted, static CAD plugin. No model-generated source or execution tools.
import { readFileSync } from 'node:fs';
export const name = 'cad-readonly-tools';
export const inject = ['tools'];
export function apply(ctx) {
  const definitions = JSON.parse(readFileSync(process.env.CAD_TOOL_SCHEMA_FILE, 'utf8'));
  const allowed = new Set(definitions.map(t => t.function.name));
  ctx.tools.guard(exec => allowed.has(exec.name) ? undefined : 'Only CAD tools are authorized');
  for (const {function: tool} of definitions) {
    ctx.tools.register({
      name: tool.name, description: tool.description, parameters: tool.parameters,
      output: {schema: {type:'string'}, render: (_args, value) => [{type:'text',text:value}]},
      async execute(args, exec) {
        const response = await fetch(process.env.CAD_BRIDGE_URL + '/cad/tool', {
          method: 'POST', headers: {'Content-Type':'application/json', Authorization:'Bearer '+process.env.CAD_BRIDGE_TOKEN},
          body: JSON.stringify({name:tool.name, arguments:args}), signal:exec.signal,
        });
        if (!response.ok) throw new Error('CAD tool bridge rejected the request');
        return await response.text();
      },
    });
  }
}
