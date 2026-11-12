// Jest setup (picked up automatically by react-scripts / craco test).
import { TextDecoder, TextEncoder } from "util";

// react-router 7 needs these; jsdom doesn't provide them.
global.TextEncoder = global.TextEncoder || TextEncoder;
global.TextDecoder = global.TextDecoder || TextDecoder;

// jsdom has no layout: the chat scroller calls element.scrollTo().
if (!Element.prototype.scrollTo) Element.prototype.scrollTo = () => {};

// Tell React this is a test environment that wraps updates in act().
global.IS_REACT_ACT_ENVIRONMENT = true;
