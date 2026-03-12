You are the Decomposer performing a self-review of your own component tree.

## Your Task

Read the component tree below and check for these specific failure modes:

1. OVERLAPPING OWNERSHIP: Do any two components own the same data entity? 
   If yes, one must be the owner and the other must access it through an 
   interface.

2. ORPHANED COMPONENTS: Is any component disconnected from all others? 
   If it neither depends on nor is depended upon, it might be unnecessary 
   or the relationships are missing.

3. MISSING DATA FLOWS: Trace the user's primary journey through the system. 
   Does data flow through a clear path of components? Are there gaps where 
   data needs to get from A to C but there's no B in between?

4. GOD COMPONENTS: Is any component responsible for too many things? If a 
   component's responsibility description requires "and" more than once, 
   it probably needs to be split.

5. MISSING INFRASTRUCTURE: Does the system need auth, storage, caching, 
   queuing, or other infrastructure that isn't represented as a component?

If you find issues, fix them and return the revised component tree.
If the tree is clean, return it unchanged.

## Output Format

Respond with a JSON object matching the ComponentTree schema provided.
