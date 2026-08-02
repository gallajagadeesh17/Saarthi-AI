// ========================================
// SAARTHI AI CHAT
// ========================================

const chatToggle = document.getElementById("chatToggle");
const chatWindow = document.getElementById("chatWindow");
const closeChat = document.getElementById("closeChat");
const chatMessages = document.getElementById("chatMessages");
const chatInput = document.getElementById("chatInput");
const sendButton = document.getElementById("sendMessage");
const typingIndicator = document.getElementById("typingIndicator");

// Open / Close Chat
chatToggle.addEventListener("click", () => {
    chatWindow.classList.toggle("hidden");

    if (!chatWindow.classList.contains("hidden")) {
        chatInput.focus();
    }
});

closeChat.addEventListener("click", () => {
    chatWindow.classList.add("hidden");
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        chatWindow.classList.add("hidden");
    }
});

// Auto Scroll
function scrollBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// ==========================
// User Bubble
// ==========================

function addUserMessage(message) {

    const div = document.createElement("div");

    div.className = "user-message";

    div.innerHTML = `
        <div class="user-bubble">
            ${message}
        </div>
    `;

    chatMessages.appendChild(div);

    scrollBottom();
}

// ==========================
// AI Bubble
// ==========================

function addAIMessage(message) {

    const div = document.createElement("div");

    div.className = "ai-message";

    div.innerHTML = `
        <div class="ai-avatar">
            🤖
        </div>

        <div class="ai-bubble">
            ${message}
        </div>
    `;

    chatMessages.appendChild(div);

    scrollBottom();

}

// ==========================
// Typing
// ==========================

function showTyping() {

    typingIndicator.classList.remove("hidden");

    scrollBottom();

}

function hideTyping() {

    typingIndicator.classList.add("hidden");

}

// ==========================
// Send Message
// ==========================

function sendMessage(){

    const message = chatInput.value.trim();

    if(message==="") return;

    addUserMessage(message);

    chatInput.value="";

    // --- Call Flask Backend ---
    showTyping();

    fetch("/api/chat", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            message: message
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! Status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        hideTyping();
        const reply = data.reply || "I'm sorry, I couldn't process that. Please try again.";
        addAIMessage(reply);
    })
    .catch(error => {
        hideTyping();
        addAIMessage("❌ Unable to contact Saarthi AI. Please check the server logs.");
        console.error('Error:', error);
    });

}

sendButton.addEventListener("click",sendMessage);

chatInput.addEventListener("keydown",(e)=>{

    if(e.key==="Enter"){

        sendMessage();

    }

});

// ==========================
// Suggestion Buttons
// ==========================

document.querySelectorAll(".suggestion").forEach(btn=>{

    btn.addEventListener("click",()=>{

        chatInput.value=btn.innerText;

        sendMessage();

    });

});

// =======================================
// Welcome Animation
// =======================================

window.addEventListener("load",()=>{

    setTimeout(()=>{

        if(chatWindow.classList.contains("hidden")) return;

    const welcomeMessage = `Hello! I'm Saarthi AI, your Sales Intelligence Assistant.
        <br><br>
        I can help you:
        <ul class="list-disc list-inside pl-2 mt-2 space-y-1 text-sm">
            <li>Generate AI meeting briefings</li>
            <li>Research companies</li>
            <li>Find the latest business news</li>
            <li>Identify opportunities and risks</li>
            <li>Prepare talking points for client meetings</li>
            <li>Guide you through the Saarthi AI platform</li>
        </ul>
        <br>How can I assist you today?`;
    addAIMessage(welcomeMessage);

    },700);

});