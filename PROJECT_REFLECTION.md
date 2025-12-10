See [README.md](README.md) for instructions.

After reading the functional requirements, I realized there were a number of design decisions that I needed to make to develop this simplified version of the shift fanout service. 

One such decision was determining how to implement the 10 minute timeout, before a second round of contact attempts should be made. I debated between using sleep() or storing a timestamp. While sleep() is a simple solution, it is very resource inefficient, as it blocks the main thread for the duration of the sleep. So instead, I chose to store a timestamp, and check the difference between the current time and the timestamp.

Additionally, to ensure that re-posting the same shift fan-out did not send duplicate notifications, I decided to use a set to store the shift IDs that have already been processed. This ensures that each shift is only processed once, even if it is re-posted multiple times.

Lastly, I made sure to filter roles before doing sending any notifications. This ensures that only caregivers with the required role are contacted for a shift.

These decisions were made by me after considering the pros and cons of each approach in a discussion with ChatGPT 5.1. After about a 20 minute discussion, I asked the model to develop a prompt to complete the assignment for Google Antigravity, containing the design insights from our conversation. I asked it to repeatedly modify different aspects of the prompt until it was satisfied with the final version.

Afterwards, I fed the prompt to Google Antigravity, and worked together with it to implement the files, run the testing suite, design new test cases, and ensure all of the functional requirements, constraints, and guidelines were met for the assignment.



